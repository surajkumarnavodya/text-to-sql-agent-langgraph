"""SQLAlchemy connection lifecycle for the user's real, configured database.

Fully config-driven: `DB_TYPE` selects the engine family (see
`SUPPORTED_DB_TYPES`), and the connection is built either from the discrete
`DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` fields or from a full
`DB_CONNECTION_STRING` override. Nothing here is hardcoded to a specific
database, host, or schema -- see `config/settings.py` for where those values
come from.

Security model: this app only ever needs read access. `get_read_only_engine()`
is the single entry point every query-executing code path (`agent/nodes.py`,
`ui/app.py`) must use. It does not *itself* strip write privileges -- no
generic, cross-database way to do that exists at the SQLAlchemy layer -- so
the real enforcement is layered:
  1. `agent/sql_validator.py` rejects any non-SELECT statement before it is
     ever sent to the database (the primary gate).
  2. The `DB_USER` in `.env` should point at a dedicated, DB-level read-only
     role. See README's "Security" section for why and how.
This module documents that requirement rather than silently assuming it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import NoSuchModuleError

from config.settings import ConfigurationError, Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DbTypeInfo:
    """Connection metadata for one supported DB_TYPE value."""

    drivername: str
    default_port: int
    sqlglot_dialect: str | None  # None means sqlglot's generic/default dialect
    driver_package: str  # pip package name, for "driver not installed" error messages


# The single source of truth mapping DB_TYPE -> SQLAlchemy driver + default
# port + the matching sqlglot dialect name (agent/sql_validator.py imports
# `get_sqlglot_dialect` below rather than duplicating this table).
SUPPORTED_DB_TYPES: dict[str, DbTypeInfo] = {
    "postgresql": DbTypeInfo("postgresql+psycopg2", 5432, "postgres", "psycopg2-binary"),
    "mysql": DbTypeInfo("mysql+pymysql", 3306, "mysql", "pymysql"),
    "mssql": DbTypeInfo("mssql+pyodbc", 1433, "tsql", "pyodbc"),
    "oracle": DbTypeInfo("oracle+oracledb", 1521, "oracle", "oracledb"),
}


def get_sqlglot_dialect(db_type: str) -> str | None:
    """Maps DB_TYPE to the sqlglot dialect name used for SQL validation/parsing.

    Returns None for an unrecognized db_type, which tells sqlglot to use its
    generic/standard-SQL dialect rather than erroring -- validation should
    still work reasonably for statement-type checking even if the dialect
    isn't one of the four officially supported ones.
    """
    info = SUPPORTED_DB_TYPES.get((db_type or "").strip().lower())
    return info.sqlglot_dialect if info else None


def _describe_target(settings: Settings) -> str:
    """Human-readable, credential-free description of the configured DB, for logging.

    Never includes the password or the full connection string -- see
    CLAUDE.md's "Security" notes on what's safe to log.
    """
    if settings.db_connection_string:
        return "<DB_CONNECTION_STRING override>"
    return f"{settings.db_type}://{settings.db_host}:{settings.db_port}/{settings.db_name}"


def build_connection_url(settings: Settings) -> URL | str:
    """Builds a SQLAlchemy connection URL from config, validating as it goes.

    If `DB_CONNECTION_STRING` is set, it's used as-is (full override) --
    everything else is ignored. Otherwise, builds a URL from the discrete
    `db_*` fields via `DB_TYPE`.

    Args:
        settings: Application settings.

    Returns:
        A `sqlalchemy.engine.URL` (or the raw override string).

    Raises:
        ConfigurationError: if `DB_TYPE` is missing/unrecognized, or a
            required field (`DB_HOST`, `DB_NAME`) is missing -- this is the
            "malformed .env fails fast" behavior: we'd rather raise here
            than let SQLAlchemy attempt a connection with a nonsensical URL
            and produce a confusing downstream error.
    """
    if settings.db_connection_string:
        return settings.db_connection_string

    db_type = settings.db_type
    if db_type not in SUPPORTED_DB_TYPES:
        raise ConfigurationError(
            f"Unsupported or missing DB_TYPE='{settings.db_type}'. Supported "
            f"values: {', '.join(sorted(SUPPORTED_DB_TYPES))}. Set DB_TYPE in "
            f".env, or set DB_CONNECTION_STRING to bypass this entirely."
        )
    info = SUPPORTED_DB_TYPES[db_type]

    missing = [
        name
        for name, value in (("DB_HOST", settings.db_host), ("DB_NAME", settings.db_name))
        if not value
    ]
    if missing:
        raise ConfigurationError(
            f"Missing required connection setting(s): {', '.join(missing)}. "
            f"Set them in .env, or set DB_CONNECTION_STRING to bypass this entirely."
        )

    query: dict[str, str] = {}
    if db_type == "mssql":
        query["driver"] = settings.db_odbc_driver

    return URL.create(
        drivername=info.drivername,
        username=settings.db_user or None,
        password=settings.db_password or None,
        host=settings.db_host,
        port=settings.db_port or info.default_port,
        database=settings.db_name,
        query=query,
    )


@lru_cache(maxsize=1)
def _cached_engine(connection_string: str) -> Engine:
    """Process-wide singleton engine, keyed by the resolved connection string.

    `lru_cache` gives connection pooling "for free": every caller within the
    process shares the same `Engine` (and thus its connection pool) instead
    of opening a fresh one per query. The cache key lives only in memory for
    this process -- it is never logged or persisted.
    """
    return create_engine(connection_string, pool_pre_ping=True, pool_recycle=1800)


def get_engine(settings: Settings | None = None) -> Engine:
    """Builds (or reuses) the SQLAlchemy engine for the configured database.

    Args:
        settings: Optional `Settings` override (mainly for tests).

    Returns:
        A pooled, reused SQLAlchemy `Engine`.

    Raises:
        ConfigurationError: see `build_connection_url`.
    """
    settings = settings or get_settings()
    url = build_connection_url(settings)
    logger.debug("Resolved database target: %s", _describe_target(settings))
    return _cached_engine(str(url))


def get_read_only_engine(settings: Settings | None = None) -> Engine:
    """Returns the engine every query-executing code path must use.

    This does not enforce read-only access by itself -- see this module's
    docstring. It exists as a named, documented call site so the intent
    ("only ever read from this database") is unambiguous at every call site,
    even though the actual enforcement is the SQL validator plus a
    DB-level read-only user.
    """
    return get_engine(settings)


class ConnectionErrorCategory(str, Enum):
    """Best-effort classification of *why* a connection attempt failed.

    Classification is heuristic (keyword-matching on the underlying driver's
    error message), because SQLAlchemy does not normalize error causes
    consistently across the four supported drivers (psycopg2, pymysql,
    pyodbc, oracledb). It covers the common cases well but is not
    authoritative -- `ConnectionTestResult.message` always includes the
    original driver error text too, so nothing is hidden behind the
    classification.
    """

    CONFIGURATION = "configuration"
    DRIVER_MISSING = "driver_missing"
    AUTH_FAILURE = "auth_failure"
    HOST_UNREACHABLE = "host_unreachable"
    DATABASE_NOT_FOUND = "database_not_found"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


_CATEGORY_GUIDANCE: dict[ConnectionErrorCategory, str] = {
    ConnectionErrorCategory.CONFIGURATION: "Check your .env file for missing or invalid DB_* settings.",
    ConnectionErrorCategory.DRIVER_MISSING: "The Python driver for this DB_TYPE isn't installed -- see requirements.txt.",
    ConnectionErrorCategory.AUTH_FAILURE: "Authentication failed -- check DB_USER and DB_PASSWORD.",
    ConnectionErrorCategory.HOST_UNREACHABLE: "Could not reach the database host -- check DB_HOST, DB_PORT, VPN, and firewall rules.",
    ConnectionErrorCategory.DATABASE_NOT_FOUND: "Check DB_NAME -- the server was reachable but that database wasn't found.",
    ConnectionErrorCategory.TIMEOUT: "Connection timed out -- check network access and that the host/port are correct.",
    ConnectionErrorCategory.UNKNOWN: "Connection failed for an unrecognized reason -- see the error detail below.",
}

_AUTH_KEYWORDS = (
    "password authentication failed",
    "login failed",
    "access denied",
    "authentication failed",
    "invalid username/password",
    "ora-01017",
)
_DB_NOT_FOUND_KEYWORDS = (
    "does not exist",
    "unknown database",
    "invalid catalog name",
    "cannot open database",
    "ora-12514",
)
_HOST_UNREACHABLE_KEYWORDS = (
    "could not connect",
    "connection refused",
    "actively refused",
    "getaddrinfo failed",
    "unreachable",
    "no route to host",
    "name or service not known",
    "no such host",
    "can't connect",
)


def _classify_error(exc: Exception) -> ConnectionErrorCategory:
    """Best-effort classification of a connection failure -- see the enum docstring."""
    if isinstance(exc, ModuleNotFoundError | NoSuchModuleError):
        return ConnectionErrorCategory.DRIVER_MISSING

    message = str(exc).lower()
    if any(keyword in message for keyword in _AUTH_KEYWORDS):
        return ConnectionErrorCategory.AUTH_FAILURE
    if any(keyword in message for keyword in _DB_NOT_FOUND_KEYWORDS):
        return ConnectionErrorCategory.DATABASE_NOT_FOUND
    if any(keyword in message for keyword in _HOST_UNREACHABLE_KEYWORDS):
        return ConnectionErrorCategory.HOST_UNREACHABLE
    if isinstance(exc, TimeoutError) or "timeout" in message or "timed out" in message:
        return ConnectionErrorCategory.TIMEOUT
    return ConnectionErrorCategory.UNKNOWN


@dataclass(frozen=True)
class ConnectionTestResult:
    """Outcome of `test_connection()`.

    Attributes:
        success: Whether the round-trip (connect + `SELECT 1`) succeeded.
        message: Human-readable summary. On failure, includes both the
            category-specific guidance and the underlying driver error text.
        category: Failure classification (None on success).
        db_version: Database version string, if it could be fetched (best
            effort; None on failure or if the DB_TYPE's version query fails).
    """

    success: bool
    message: str
    category: ConnectionErrorCategory | None = None
    db_version: str | None = None


_VERSION_QUERIES: dict[str, str] = {
    "postgresql": "SELECT version()",
    "mysql": "SELECT version()",
    "mssql": "SELECT @@VERSION",
    "oracle": "SELECT banner FROM v$version WHERE rownum = 1",
}


def _fetch_db_version(connection, dialect_name: str) -> str | None:
    """Best-effort dialect-specific version query; None if unsupported or it fails."""
    query = _VERSION_QUERIES.get(dialect_name)
    if not query:
        return None
    try:
        row = connection.execute(text(query)).fetchone()
        return str(row[0]).strip() if row and row[0] is not None else None
    except Exception:  # noqa: BLE001 - version string is a nice-to-have, never fatal
        return None


def test_connection(settings: Settings | None = None) -> ConnectionTestResult:
    """Performs a lightweight round-trip (`SELECT 1`) against the configured database.

    This is the single source of truth both `scripts/test_db_connection.py`
    and the Streamlit UI's "Test Connection" button and startup check use --
    they render `ConnectionTestResult` differently, but never re-implement
    the classification logic.

    Args:
        settings: Optional `Settings` override (mainly for tests).

    Returns:
        A `ConnectionTestResult`. Never raises -- all failure modes are
        captured in the result instead, since this function's whole purpose
        is to be safely callable from a UI event handler.
    """
    settings = settings or get_settings()
    target = _describe_target(settings)

    try:
        engine = get_engine(settings)
    except ConfigurationError as exc:
        logger.warning("Connection test failed (configuration): %s", exc)
        return ConnectionTestResult(
            success=False,
            message=f"{_CATEGORY_GUIDANCE[ConnectionErrorCategory.CONFIGURATION]} ({exc})",
            category=ConnectionErrorCategory.CONFIGURATION,
        )

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            db_version = _fetch_db_version(connection, engine.dialect.name)
        logger.info("Connection test succeeded for %s", target)
        return ConnectionTestResult(
            success=True, message="Connection successful.", db_version=db_version
        )
    except Exception as exc:  # noqa: BLE001 - deliberately broad; classified below, never re-raised
        category = _classify_error(exc)
        logger.warning(
            "Connection test failed for %s (category=%s): %s", target, category.value, exc
        )
        guidance = _CATEGORY_GUIDANCE[category]
        return ConnectionTestResult(
            success=False,
            message=f"{guidance} ({exc.__class__.__name__}: {exc})",
            category=category,
        )
