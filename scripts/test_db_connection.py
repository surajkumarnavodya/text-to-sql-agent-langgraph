"""Standalone entry point: verify your DB_* connection settings before booting the app.

Usage (from repo root, with the venv activated):

    python scripts\\test_db_connection.py

Prints a clear pass/fail, the database version, and a table count on
success; a readable error (auth failure, host unreachable, wrong DB name,
missing driver, etc. -- not a raw stack trace) on failure. Exits 0 on
success, 1 on failure, so it's usable in a pre-flight check / CI step too.

Read-only: this script never writes to the database. Introspection only
issues metadata queries (information_schema/catalog reads).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import ConfigurationError, get_settings  # noqa: E402
from db.connection import get_read_only_engine, test_connection  # noqa: E402
from db.schema_introspection import introspect_schema  # noqa: E402


def main() -> int:
    settings = get_settings()

    print(f"DB_TYPE: {settings.db_type or '(not set)'}")
    print(f"DB_HOST: {settings.db_host or '(not set)'}")
    print(f"DB_NAME: {settings.db_name or '(not set)'}")
    print(f"DB_SCHEMA: {settings.db_schema or '(default)'}")
    print()

    result = test_connection(settings)
    if not result.success:
        print(f"FAIL: {result.message}")
        if result.category:
            print(f"  category: {result.category.value}")
        return 1

    print("PASS: connection successful.")
    if result.db_version:
        print(f"  DB version: {result.db_version}")

    try:
        engine = get_read_only_engine(settings)
        tables = introspect_schema(engine, schema=settings.db_schema)
    except ConfigurationError as exc:
        # Shouldn't happen if test_connection() already succeeded, but keep
        # this readable rather than letting a stack trace through regardless.
        print(f"  Connected, but schema introspection failed: {exc}")
        return 1

    print(f"  Tables visible: {len(tables)}")
    if tables:
        preview = ", ".join(t.table_name for t in tables[:10])
        suffix = ", ..." if len(tables) > 10 else ""
        print(f"  ({preview}{suffix})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
