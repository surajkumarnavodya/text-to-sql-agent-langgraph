"""Registers a published onboarding database as a configured `DB_CONNECTIONS` entry.

Publishing an onboarding job (`api/onboarding.py`'s publish route) makes its
semantic contract available, but chat only ever routes questions to databases
listed in `DB_CONNECTIONS` in `.env`. This module closes that gap: it writes
the same `DB_<NAME>_*` block the hand-configured databases use, appends the
name to `DB_CONNECTIONS`, and applies the change to the running process so the
next question can already be routed to it -- no restart.

Deliberate, disclosed tradeoff: the connection password is written to `.env`
in plaintext, exactly as the existing entries already are. This is an explicit
operator decision (`.env` is gitignored), and it reverses onboarding's earlier
"a connection secret is never persisted" default for published databases only.
Passwords are never logged.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import dotenv_values

from config.settings import PROJECT_ROOT, _connection_env_prefix, get_settings

logger = logging.getLogger(__name__)

DEFAULT_ENV_PATH: Path = PROJECT_ROOT / ".env"
_ENV_LOCK = threading.Lock()
_DEFAULT_ODBC_DRIVER = "ODBC Driver 17 for SQL Server"


@dataclass(frozen=True)
class PublishedDatabase:
    """The connection details of one published onboarding job, as `.env` needs them."""

    label: str
    job_id: str
    db_type: str
    host: str
    port: int | None
    name: str
    user: str | None
    password: str | None
    schema: str | None
    odbc_driver: str = _DEFAULT_ODBC_DRIVER


@dataclass(frozen=True)
class RegistrationResult:
    """What `register_published_database` did: the connection name, and whether it wrote anything.

    `previous_env_text` and `previous_connections` hold the state before the write,
    so `rollback_registration` can restore it exactly if a later step fails.
    """

    connection_name: str
    newly_added: bool
    entries: dict[str, str]
    connection_names: list[str]
    previous_env_text: str | None = None
    previous_connections: str | None = None


def slugify_connection_name(label: str) -> str:
    """Turns a human-entered job label into a connection name (letters, digits, underscores)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()
    return (slug or "database")[:40]


def _quote(value: str) -> str:
    """Double-quotes a `.env` value, escaping backslashes and quotes.

    Always quoting keeps values such as `(localdb)\\MSSQLLocalDB` and passwords
    containing `#`, spaces, or `=` intact through python-dotenv.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _is_named_instance(host: str) -> bool:
    return "\\" in host


def _entries_for(db: PublishedDatabase, prefix: str) -> dict[str, str]:
    """The `DB_<PREFIX>_*` assignments for one database, in the same shape as the hand-written ones."""
    entries: dict[str, str] = {
        f"DB_{prefix}_TYPE": db.db_type,
        f"DB_{prefix}_HOST": db.host,
        f"DB_{prefix}_NAME": db.name,
        f"DB_{prefix}_ODBC_DRIVER": db.odbc_driver,
    }
    # A named/LocalDB instance has no TCP port (see db.connection.build_connection_url).
    if db.port is not None and not _is_named_instance(db.host):
        entries[f"DB_{prefix}_PORT"] = str(db.port)
    if db.user:
        entries[f"DB_{prefix}_USER"] = db.user
    if db.password:
        entries[f"DB_{prefix}_PASSWORD"] = db.password
    if db.schema:
        entries[f"DB_{prefix}_SCHEMA"] = db.schema
    return entries


def _existing_connection_names(values: dict[str, str | None]) -> list[str]:
    raw = values.get("DB_CONNECTIONS") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _find_existing(db: PublishedDatabase, values: dict[str, str | None]) -> str | None:
    """Returns the name of an already-configured connection to the same server and database."""
    for name in _existing_connection_names(values):
        prefix = _connection_env_prefix(name)
        same_type = (values.get(f"DB_{prefix}_TYPE") or "").lower() == db.db_type.lower()
        same_host = (values.get(f"DB_{prefix}_HOST") or "").lower() == db.host.lower()
        same_name = (values.get(f"DB_{prefix}_NAME") or "").lower() == db.name.lower()
        if same_type and same_host and same_name:
            return name
    return None


def _unique_connection_name(base: str, values: dict[str, str | None], taken: set[str]) -> str:
    """`base`, or `base_2`, `base_3`, ... until neither the name nor its env prefix is taken."""
    candidate, counter = base, 2
    while True:
        prefix = _connection_env_prefix(candidate)
        name_taken = candidate in taken or f"DB_{prefix}_TYPE" in values
        if not name_taken:
            return candidate
        candidate = f"{base}_{counter}"
        counter += 1


def register_published_database(
    db: PublishedDatabase,
    env_path: Path | None = None,
) -> RegistrationResult:
    """Adds a published database to `.env` as a `DB_CONNECTIONS` entry.

    Idempotent per target: if a configured connection already points at the same
    type, host and database name, that name is returned and nothing is written.

    The `.env` file is the durable record. Call `apply_to_runtime` so the running
    process sees the entry, then the caller's follow-up work (schema indexing,
    which looks the name up in live settings). If that follow-up fails, call
    `rollback_registration` to restore the previous state.

    Raises:
        OSError: if `.env` cannot be read or written.
    """
    if env_path is None:
        env_path = DEFAULT_ENV_PATH
    with _ENV_LOCK:
        values: dict[str, str | None] = (
            dict(dotenv_values(env_path)) if env_path.exists() else {}
        )
        existing = _find_existing(db, values)
        if existing is not None:
            return RegistrationResult(
                connection_name=existing,
                newly_added=False,
                entries={},
                connection_names=_existing_connection_names(values),
            )

        names = _existing_connection_names(values)
        name = _unique_connection_name(slugify_connection_name(db.label), values, set(names))
        prefix = _connection_env_prefix(name)
        entries = _entries_for(db, prefix)
        connection_names = [*names, name]

        previous_env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else None
        previous_connections = values.get("DB_CONNECTIONS")
        lines = previous_env_text.splitlines() if previous_env_text is not None else []
        new_connections_line = f"DB_CONNECTIONS={','.join(connection_names)}"
        replaced = False
        for index, line in enumerate(lines):
            if re.match(r"^\s*DB_CONNECTIONS\s*=", line):
                lines[index] = new_connections_line
                replaced = True
                break
        if not replaced:
            lines.append(new_connections_line)

        lines.append("")
        lines.append(f"# Registered by onboarding job {db.job_id} on {date.today().isoformat()}")
        for key, value in entries.items():
            lines.append(f"{key}={_quote(value)}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        logger.info(
            "[onboarding] registered published database as DB_CONNECTIONS entry %r "
            "(%s, database %r) in .env",
            name,
            db.db_type,
            db.name,
        )
        return RegistrationResult(
            connection_name=name,
            newly_added=True,
            entries=entries,
            connection_names=connection_names,
            previous_env_text=previous_env_text,
            previous_connections=previous_connections,
        )


def apply_to_runtime(result: RegistrationResult) -> None:
    """Makes a registration visible to this running process without a restart.

    Sets the new variables in `os.environ` (the same place `load_dotenv` writes
    them at startup) and clears the cached `Settings`, so the next
    `get_settings()` call rebuilds `Settings.databases` including the new name.
    """
    if not result.newly_added:
        return
    os.environ["DB_CONNECTIONS"] = ",".join(result.connection_names)
    os.environ.update(result.entries)
    get_settings.cache_clear()


def rollback_registration(result: RegistrationResult, env_path: Path | None = None) -> None:
    """Undoes a registration that was written but could not be completed.

    Restores the `.env` file's previous text and `DB_CONNECTIONS` in the running
    process, and removes the new entry's variables from `os.environ`. A no-op for
    a registration that wrote nothing.
    """
    if not result.newly_added:
        return
    if env_path is None:
        env_path = DEFAULT_ENV_PATH
    with _ENV_LOCK:
        if result.previous_env_text is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(result.previous_env_text, encoding="utf-8")
        for key in result.entries:
            os.environ.pop(key, None)
        if result.previous_connections is None:
            os.environ.pop("DB_CONNECTIONS", None)
        else:
            os.environ["DB_CONNECTIONS"] = result.previous_connections
        get_settings.cache_clear()
        logger.warning("[onboarding] rolled back chat registration of %r", result.connection_name)
