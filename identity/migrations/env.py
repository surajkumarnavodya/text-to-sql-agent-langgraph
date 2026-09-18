"""Alembic environment for the identity database -- the one database in
this repo with real, version-controlled migrations (see
`identity/__init__.py`'s module docstring for why).

Deliberately reads its connection string from `config.settings.get_settings
().auth_database_url` (a `SecretStr`, `.env`-driven) rather than
`alembic.ini`'s own `sqlalchemy.url` -- keeps the real connection string
out of a checked-in config file, matching this project's "never hardcode a
connection detail" convention everywhere else.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# `alembic -c identity/alembic.ini ...` does not guarantee the repo root is
# on sys.path, same reason every scripts/*.py entry point does this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from identity.models import Base  # noqa: E402

from config.settings import get_settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _connection_url() -> str:
    settings = get_settings()
    if settings.auth_database_url is None:
        raise RuntimeError(
            "AUTH_DATABASE_URL is not set in .env -- required to run identity "
            "database migrations. See .env.example's identity/auth section."
        )
    return settings.auth_database_url.get_secret_value()


def run_migrations_offline() -> None:
    """Emits SQL to stdout without a live DB connection (`alembic upgrade
    head --sql`) -- standard Alembic offline mode, useful for review before
    applying to a production database by hand."""
    context.configure(
        url=_connection_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _connection_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
