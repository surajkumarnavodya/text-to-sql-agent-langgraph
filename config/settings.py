"""Central application settings.

Every configurable path, model name, and limit used anywhere in the project
is defined here and nowhere else, sourced from environment variables (loaded
from a local `.env` file via python-dotenv if present). Import `get_settings()`
rather than reading `os.environ` directly elsewhere in the codebase.

Database connectivity is fully config-driven (see the `db_*` fields below) --
there is no hardcoded connection string or sample schema anywhere in the
project. `db/connection.py` is the only other module allowed to interpret
these `db_*` fields into an actual SQLAlchemy engine/URL.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from security.secrets import SecretStr, as_secret

# Project root is the parent of this file's parent (config/settings.py -> repo root).
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

# Load .env once at import time. Safe to call even if .env doesn't exist yet
# (e.g. a fresh checkout that hasn't run `Copy-Item .env.example .env`).
load_dotenv(PROJECT_ROOT / ".env")


class ConfigurationError(Exception):
    """Raised when a config value is present but malformed.

    Deliberately distinct from a missing value (which callers may have a
    sensible default for): this means "the user set something, and it's
    wrong" -- e.g. DB_PORT=notanumber -- which should fail fast and loudly
    rather than silently falling back to a default and connecting to the
    wrong thing.
    """


def _env_str(name: str, default: str) -> str:
    """Read a string environment variable, falling back to `default`."""
    return os.environ.get(name, default)


def _env_optional_str(name: str) -> str | None:
    """Read an optional string environment variable; None if unset/blank."""
    raw = os.environ.get(name)
    return raw if raw and raw.strip() else None


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to `default`."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable ("true"/"1"/"yes", case-insensitive), falling back to `default`."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes")


def _env_optional_int_strict(name: str) -> int | None:
    """Read an optional integer environment variable.

    Unlike `_env_int`, this does not silently fall back to a default when
    the value is malformed -- a *present but invalid* value (e.g.
    `DB_PORT=abc`) raises `ConfigurationError` immediately, since that's a
    real mistake in `.env`, not an intentional "use the default" signal.
    Absent/blank is fine and returns None (caller decides the fallback,
    e.g. a per-DB_TYPE default port).
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name}='{raw}' is not a valid integer. Fix it in .env.") from exc


def _resolve_path(raw: str) -> Path:
    """Resolve a possibly-relative path from .env against the project root."""
    path = Path(raw)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of application configuration.

    Attributes:
        ollama_host: Base URL of the local Ollama server.
        ollama_model: Name of the Ollama model to use for SQL generation.
            Swap this (via OLLAMA_MODEL in .env) to try sqlcoder, duckdb-nsql, etc.
        ollama_request_timeout_seconds: Per-request timeout for calls to Ollama.
        db_type: Target database engine, e.g. "postgresql", "mssql", "mysql",
            "oracle". Interpreted by `db/connection.py` -- see
            `db.connection.SUPPORTED_DB_TYPES` for the full list.
        db_host: Database host/server name.
        db_port: Database port. None means "use the DB_TYPE's default port".
        db_name: Database (catalog) name.
        db_user: Database login username. Should be a dedicated read-only
            account -- see README's "Security" section.
        db_password: Database login password. Never logged. Stored as a
            `security.secrets.SecretStr` (coerced in `__post_init__` below,
            regardless of whether the caller passed a plain `str` or an
            already-wrapped value) -- a real `str` for every purpose that
            needs the actual value, but its `repr()`/`%r` output is
            redacted, so an accidental `logger.debug("%r", settings)` or a
            traceback's local-variable dump can't leak it.
        db_connection_string: Optional full SQLAlchemy connection string,
            used as-is instead of building one from the discrete db_* fields
            above if set. Also coerced to `SecretStr` -- it may itself embed
            a password.
        db_schema: Optional schema name to restrict introspection to (so
            only that schema's tables are exposed to the LLM). None means
            "use the database's default schema".
        db_odbc_driver: ODBC driver name for DB_TYPE=mssql, e.g.
            "ODBC Driver 17 for SQL Server". Ignored for other DB_TYPEs.
        chroma_persist_dir: Directory where ChromaDB persists its index.
        chroma_collection_name: Name of the Chroma collection holding schema DDL.
        embedding_model_name: sentence-transformers model used for embeddings.
        schema_top_k: Number of most-relevant tables to retrieve per question.
        max_retries: Max self-correction retries in the LangGraph agent loop.
        max_result_rows: Row cap applied to every executed query.
        query_timeout_seconds: Wall-clock timeout for query execution.
        llm_max_tokens: Max tokens the LLM may generate per call (sandboxing).
        insight_max_tokens: Max tokens the LLM may generate for the post-query
            plain-English insight sentence (see `agent.llm_client.
            generate_insight_from_llm`) -- deliberately small and separate
            from `llm_max_tokens`, since this is 1-2 sentences, not SQL.
        max_question_length: Maximum accepted raw length (characters) of a
            user's typed question, enforced by `agent.input_guard.
            check_input` before any normalization or LLM call -- see
            CLAUDE.md's adversarial-input-hardening notes.
        question_rate_limit_per_minute: Max question submissions per minute,
            per Streamlit session -- see `agent.rate_limit`. A basic,
            in-memory safeguard for local/single-user use, not a
            multi-tenant rate limiter (see SECURITY.md).
        llm_call_rate_limit_per_minute: Max LLM *generation* calls per
            minute, process-wide -- deliberately stricter than and separate
            from `question_rate_limit_per_minute`, since a single
            question's self-correction retries (up to `max_retries + 1`
            calls) could otherwise multiply load well past what the
            question-level limit alone would suggest.
        cost_estimation_enabled: Whether `db.query_cost` runs a proactive,
            non-executing cost estimate (EXPLAIN/SHOWPLAN) before running a
            validated query. Fails open regardless (see
            `cost_estimation_timeout_seconds`); this flag is an extra,
            simpler off-switch if it's ever undesirable for a given setup.
        cost_estimation_timeout_seconds: Short timeout for the plan-only
            EXPLAIN/SHOWPLAN call itself -- getting an execution plan
            should be fast; if it isn't, the check is abandoned and the
            query proceeds to the existing timeout-based protection rather
            than blocking the pipeline on plan estimation.
        cost_moderate_row_threshold: Estimated row count above which a
            query gets a "this may take a moment" notice but still runs.
        cost_high_row_threshold: Estimated row count above which a query is
            not run at all -- treated as a retryable error fed back to
            `generate_sql`, same as any other correctable mistake.
        log_level: Root logging level, e.g. "INFO", "DEBUG".
        log_redaction_level: How much result-set shape `observability.
            redaction.summarize_result_for_log` includes when logging a
            query result -- "standard" (default) logs row/column counts and
            column *names* (never cell values, which are never logged at
            any level); "strict" drops column names too, leaving only the
            counts. Column names can themselves be sensitive in some
            schemas (e.g. `ssn`, `salary`) even though the data isn't
            logged, hence the stricter option rather than treating
            "no cell values" as sufficient on its own.
        api_auth_token: Optional bearer token required on every `api/`
            request (`Authorization: Bearer <token>`) when set. None
            (default, unset in `.env`) means the API has no auth check of
            its own -- a deliberate, documented "lightweight hook, not a
            full auth system" posture (see `docs/DEPLOYMENT.md`): anything
            beyond local/trusted-network use should sit behind a real
            authenticating reverse proxy regardless of whether this is set.
            Stored as `SecretStr` for the same reason `db_password` is.
        project_root: Absolute path to the repository root.
    """

    ollama_host: str
    ollama_model: str
    ollama_request_timeout_seconds: int

    db_type: str
    db_host: str | None
    db_port: int | None
    db_name: str | None
    db_user: str | None
    db_password: SecretStr | None
    db_connection_string: SecretStr | None
    db_schema: str | None
    db_odbc_driver: str

    chroma_persist_dir: Path
    chroma_collection_name: str
    embedding_model_name: str
    schema_top_k: int
    max_retries: int
    max_result_rows: int
    query_timeout_seconds: int
    llm_max_tokens: int
    insight_max_tokens: int
    max_question_length: int
    question_rate_limit_per_minute: int
    llm_call_rate_limit_per_minute: int
    cost_estimation_enabled: bool
    cost_estimation_timeout_seconds: int
    cost_moderate_row_threshold: int
    cost_high_row_threshold: int
    log_level: str
    log_redaction_level: str
    api_auth_token: SecretStr | None = None
    project_root: Path = PROJECT_ROOT

    def __post_init__(self) -> None:
        """Coerces secret fields and validates security-relevant values.

        Runs on *every* construction of `Settings` -- not just the one path
        through `get_settings()` below -- so both protections apply
        uniformly, including to every test in this codebase that builds a
        `Settings(...)` directly.

        `object.__setattr__` is required because `Settings` is a frozen
        dataclass (immutable after construction is the point -- see the
        class docstring); `__post_init__` is the one place frozen dataclass
        fields may still be set, exactly for this kind of post-construction
        normalization.
        """
        object.__setattr__(self, "db_password", as_secret(self.db_password))
        object.__setattr__(self, "db_connection_string", as_secret(self.db_connection_string))
        object.__setattr__(self, "api_auth_token", as_secret(self.api_auth_token))
        self._validate_security_settings()

    def _validate_security_settings(self) -> None:
        """Fails fast on a nonsensical security-relevant value.

        Mirrors the existing "malformed value fails fast" philosophy this
        module already applies to `DB_PORT` (see
        `_env_optional_int_strict`) -- a *present* value that's negative,
        zero, or internally inconsistent (e.g. the "run a query without
        blocking" threshold set higher than the "block this query"
        threshold) is a real misconfiguration, not a style choice, and
        should raise here rather than silently produce a security control
        that doesn't actually do what its name says.
        """
        positive_fields = (
            ("MAX_RETRIES", self.max_retries),
            ("MAX_RESULT_ROWS", self.max_result_rows),
            ("QUERY_TIMEOUT_SECONDS", self.query_timeout_seconds),
            ("LLM_MAX_TOKENS", self.llm_max_tokens),
            ("INSIGHT_MAX_TOKENS", self.insight_max_tokens),
            ("MAX_QUESTION_LENGTH", self.max_question_length),
            ("QUESTION_RATE_LIMIT_PER_MINUTE", self.question_rate_limit_per_minute),
            ("LLM_CALL_RATE_LIMIT_PER_MINUTE", self.llm_call_rate_limit_per_minute),
            ("COST_ESTIMATION_TIMEOUT_SECONDS", self.cost_estimation_timeout_seconds),
            ("COST_MODERATE_ROW_THRESHOLD", self.cost_moderate_row_threshold),
            ("COST_HIGH_ROW_THRESHOLD", self.cost_high_row_threshold),
        )
        for name, value in positive_fields:
            if value <= 0:
                raise ConfigurationError(
                    f"{name}={value} is not valid -- it must be a positive number. "
                    f"Fix it in .env (or remove it to use the default)."
                )

        if self.cost_moderate_row_threshold >= self.cost_high_row_threshold:
            raise ConfigurationError(
                f"COST_MODERATE_ROW_THRESHOLD ({self.cost_moderate_row_threshold}) must be "
                f"strictly less than COST_HIGH_ROW_THRESHOLD ({self.cost_high_row_threshold}) "
                f"-- otherwise a query is never classified 'moderate', only 'low' or 'high'. "
                f"Fix both in .env."
            )

        if self.log_redaction_level not in ("standard", "strict"):
            raise ConfigurationError(
                f"LOG_REDACTION_LEVEL={self.log_redaction_level!r} is not valid -- it must be "
                f"'standard' or 'strict'. Fix it in .env (or remove it to use the default)."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings instance, built from environment variables.

    Cached with `lru_cache` so repeated calls (e.g. from every agent node) do
    not re-parse the environment; this mirrors Streamlit's own
    `@st.cache_resource` pattern for one-time setup.

    Raises:
        ConfigurationError: if a present-but-malformed value is found (e.g.
            DB_PORT is set but isn't a number). Missing values are not an
            error here -- `db/connection.py` validates *combinations* of
            db_* fields (e.g. "DB_TYPE is set but DB_HOST is missing") at
            the point something actually tries to connect, since plenty of
            non-DB functionality (linting, non-DB tests, etc.) shouldn't
            require a fully-configured database connection just to import
            this module.
    """
    settings = Settings(
        ollama_host=_env_str("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=_env_str("OLLAMA_MODEL", "llama3.1:8b"),
        ollama_request_timeout_seconds=_env_int("OLLAMA_REQUEST_TIMEOUT_SECONDS", 60),
        db_type=_env_str("DB_TYPE", "").strip().lower(),
        db_host=_env_optional_str("DB_HOST"),
        db_port=_env_optional_int_strict("DB_PORT"),
        db_name=_env_optional_str("DB_NAME"),
        db_user=_env_optional_str("DB_USER"),
        db_password=as_secret(_env_optional_str("DB_PASSWORD")),
        db_connection_string=as_secret(_env_optional_str("DB_CONNECTION_STRING")),
        db_schema=_env_optional_str("DB_SCHEMA"),
        db_odbc_driver=_env_str("DB_ODBC_DRIVER", "ODBC Driver 17 for SQL Server"),
        chroma_persist_dir=_resolve_path(_env_str("CHROMA_PERSIST_DIR", "./embeddings/.chroma")),
        chroma_collection_name=_env_str("CHROMA_COLLECTION_NAME", "schema_ddl"),
        embedding_model_name=_env_str("EMBEDDING_MODEL_NAME", "all-MiniLM-L6-v2"),
        schema_top_k=_env_int("SCHEMA_TOP_K", 4),
        max_retries=_env_int("MAX_RETRIES", 3),
        max_result_rows=_env_int("MAX_RESULT_ROWS", 1000),
        query_timeout_seconds=_env_int("QUERY_TIMEOUT_SECONDS", 15),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 1024),
        insight_max_tokens=_env_int("INSIGHT_MAX_TOKENS", 120),
        max_question_length=_env_int("MAX_QUESTION_LENGTH", 500),
        question_rate_limit_per_minute=_env_int("QUESTION_RATE_LIMIT_PER_MINUTE", 10),
        llm_call_rate_limit_per_minute=_env_int("LLM_CALL_RATE_LIMIT_PER_MINUTE", 20),
        cost_estimation_enabled=_env_bool("COST_ESTIMATION_ENABLED", True),
        cost_estimation_timeout_seconds=_env_int("COST_ESTIMATION_TIMEOUT_SECONDS", 3),
        cost_moderate_row_threshold=_env_int("COST_MODERATE_ROW_THRESHOLD", 50_000),
        cost_high_row_threshold=_env_int("COST_HIGH_ROW_THRESHOLD", 1_000_000),
        log_level=_env_str("LOG_LEVEL", "INFO"),
        log_redaction_level=_env_str("LOG_REDACTION_LEVEL", "standard").strip().lower(),
        api_auth_token=as_secret(_env_optional_str("API_AUTH_TOKEN")),
    )
    return settings


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once, in a format useful for terminal debugging.

    Called from entry points (scripts, ui/app.py, tests) rather than at
    import time, so importing this module never has the side effect of
    reconfiguring a caller's logging setup.
    """
    resolved_level = (level or get_settings().log_level).upper()
    logging.basicConfig(
        level=getattr(logging, resolved_level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
