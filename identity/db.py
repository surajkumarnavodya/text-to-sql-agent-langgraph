"""Pooled engine + session factory for the identity database.

Mirrors `moderation/store.py`'s explicit-pool `@cache`-decorated pattern
(the more current one of this codebase's two dedicated-store precedents --
see that module's own docstring) rather than `rag/store.py`'s now-stale
unconfigured-pool version: register/login/refresh/history-write traffic can
be frequent relative to this app's other, mostly-read database traffic, so
`pool_size`/`max_overflow` are explicit settings
(`Settings.auth_database_pool_size`/`auth_database_max_overflow`), not left
on SQLAlchemy's own defaults of 5/10.
"""

from __future__ import annotations

from functools import cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import Settings, get_settings
from identity.exceptions import IdentityNotConfiguredError


def _require_connection_string(settings: Settings) -> str:
    if not settings.auth_database_url:
        raise IdentityNotConfiguredError(
            "Local authentication is enabled (LOCAL_AUTH_ENABLED=true) but "
            "AUTH_DATABASE_URL is not set in .env. Set it to a PostgreSQL "
            "connection string -- see .env.example's identity/auth section."
        )
    return settings.auth_database_url.get_secret_value()


@cache
def _cached_identity_engine(
    connection_string: str, pool_size: int, max_overflow: int, pool_recycle: int
) -> Engine:
    """Process-wide engine cache, mirroring `db.connection._cached_engine`/
    `moderation.store._cached_moderation_engine`. Cache key includes the
    pool kwargs (not just the connection string) so a `.env` change to any
    of them takes effect on process restart rather than silently reusing a
    stale pool configuration.
    """
    return create_engine(
        connection_string,
        pool_pre_ping=True,
        pool_recycle=pool_recycle,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


def get_identity_engine(settings: Settings | None = None) -> Engine:
    """Returns the (cached, pooled) engine for the identity database.

    Raises:
        IdentityNotConfiguredError: if `AUTH_DATABASE_URL` is unset --
            `config.settings.Settings._validate_local_auth_requires_database`
            already refuses to start with `LOCAL_AUTH_ENABLED=true` and no
            database configured, so this is only reachable via a direct,
            out-of-band call (e.g. a test) that bypassed that startup check.
    """
    settings = settings or get_settings()
    connection_string = _require_connection_string(settings)
    return _cached_identity_engine(
        connection_string,
        settings.auth_database_pool_size,
        settings.auth_database_max_overflow,
        settings.auth_database_pool_recycle_seconds,
    )


@cache
def _cached_session_factory(engine: Engine) -> sessionmaker[Session]:
    """One `sessionmaker` per distinct engine -- `Engine` is hashable by
    identity, and `get_identity_engine()` already returns the same cached
    `Engine` instance for a given connection string, so this stays a
    process-wide singleton exactly like the engine itself."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_identity_session(settings: Settings | None = None) -> Session:
    """Returns a new `Session` bound to the identity engine.

    Callers are responsible for closing it (a `with get_identity_session() as
    session:` block, since `Session` is a context manager) -- this mirrors
    every other DB-backed module in this codebase using a plain `engine
    .connect()`/`engine.begin()` context manager rather than a framework-
    managed request-scoped session.
    """
    engine = get_identity_engine(settings)
    return _cached_session_factory(engine)()
