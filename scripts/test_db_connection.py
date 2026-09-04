"""Standalone entry point: verify your DB_* connection settings before booting the app.

Usage (from repo root, with the venv activated):

    python scripts\\test_db_connection.py

Checks every configured database (`Settings.databases` -- a plain
single-database `.env` has exactly one, named "default"; a multi-database
`.env` sets `DB_CONNECTIONS=name1,name2,...`), printing a clear pass/fail,
the database version, and a table count per connection on success; a
readable error (auth failure, host unreachable, wrong DB name, missing
driver, etc. -- not a raw stack trace) on failure. Exits 0 only if every
configured connection passed, 1 if any failed, so it's usable in a
pre-flight check / CI step too.

Read-only: this script never writes to any database. Introspection only
issues metadata queries (information_schema/catalog reads).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import ConfigurationError, get_settings  # noqa: E402
from db.connection import (  # noqa: E402
    check_write_privileges,
    get_read_only_engine,
    test_connection,
)
from db.schema_introspection import introspect_schema  # noqa: E402


def _check_one(config) -> bool:
    print(f"DB_TYPE: {config.db_type or '(not set)'}")
    print(f"DB_HOST: {config.db_host or '(not set)'}")
    print(f"DB_NAME: {config.db_name or '(not set)'}")
    print(f"DB_SCHEMA: {config.db_schema or '(default)'}")
    print()

    result = test_connection(config)
    if not result.success:
        print(f"FAIL: {result.message}")
        if result.category:
            print(f"  category: {result.category.value}")
        return False

    print("PASS: connection successful.")
    if result.db_version:
        print(f"  DB version: {result.db_version}")

    try:
        engine = get_read_only_engine(config)
        tables = introspect_schema(engine, schema=config.db_schema)
    except ConfigurationError as exc:
        # Shouldn't happen if test_connection() already succeeded, but keep
        # this readable rather than letting a stack trace through regardless.
        print(f"  Connected, but schema introspection failed: {exc}")
        return False

    print(f"  Tables visible: {len(tables)}")
    if tables:
        preview = ", ".join(t.table_name for t in tables[:10])
        suffix = ", ..." if len(tables) > 10 else ""
        print(f"  ({preview}{suffix})")

    # Best-effort, warning-only least-privilege check (see
    # db.connection.check_write_privileges' docstring) -- never a reason
    # this script exits non-zero; the connection itself already passed.
    privilege_check = check_write_privileges(engine, config)
    if privilege_check.checked and privilege_check.has_write_privileges:
        print()
        print(f"  WARNING: {privilege_check.message}")

    return True


def main() -> int:
    settings = get_settings()
    multi = len(settings.databases) > 1

    all_passed = True
    for index, config in enumerate(settings.databases):
        if multi:
            if index:
                print()
            print(f"=== {config.name} ===")
        if not _check_one(config):
            all_passed = False

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
