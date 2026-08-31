"""Manual end-to-end integration check against your REAL, configured database.

Not part of the `pytest` suite (see `tests/` -- everything there is mocked,
no real DB required) and never run by CI. Run this yourself, on demand, when
you want to confirm the full path actually works against your database:
connection -> schema introspection -> embeddings -> retrieval -> a real
read-only query execution.

Read-only: this script never writes to the database. The one query it runs
(`SELECT 1`, plus whatever `test_db_connection.py`-style checks it reuses)
is validated through the same `agent.sql_validator` allowlist as everything
else in the app.

Usage (from repo root, with the venv activated and a real .env configured):

    python scripts\\integration_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.sql_validator import enforce_row_limit, validate_sql  # noqa: E402
from config.settings import ConfigurationError, get_settings  # noqa: E402
from db.connection import get_read_only_engine, get_sqlglot_dialect, test_connection  # noqa: E402
from db.schema_introspection import introspect_schema  # noqa: E402
from embeddings.retriever import retrieve_relevant_schema  # noqa: E402
from embeddings.schema_indexer import build_index  # noqa: E402


def _step(name: str) -> None:
    print(f"\n--- {name} ---")


def main() -> int:
    settings = get_settings()

    _step("1. Connection")
    result = test_connection(settings)
    if not result.success:
        print(f"FAIL: {result.message}")
        return 1
    print(f"OK: connected. DB version: {result.db_version or '(unknown)'}")

    _step("2. Schema introspection")
    try:
        engine = get_read_only_engine(settings)
    except ConfigurationError as exc:
        print(f"FAIL: {exc}")
        return 1
    tables = introspect_schema(engine, schema=settings.db_schema)
    if not tables:
        print("FAIL: no tables visible. Check DB_SCHEMA and the DB user's privileges.")
        return 1
    print(
        f"OK: {len(tables)} table(s) introspected: {', '.join(t.table_name for t in tables[:10])}"
    )

    _step("3. Embeddings index")
    count = build_index(tables, settings=settings)
    print(f"OK: {count} table(s) embedded/cached.")

    _step("4. Schema retrieval")
    sample_question = f"Show me some rows from {tables[0].table_name}"
    retrieved = retrieve_relevant_schema(
        sample_question, top_k=min(settings.schema_top_k, len(tables))
    )
    if not retrieved:
        print("FAIL: retrieval returned no tables for a sample question.")
        return 1
    print(
        f"OK: retrieved {len(retrieved)} table(s) for {sample_question!r}: "
        f"{[t['table_name'] for t in retrieved]}"
    )

    _step("5. End-to-end read-only query execution")
    dialect = get_sqlglot_dialect(settings.db_type)
    sample_sql = f"SELECT * FROM {tables[0].table_name}"
    validation = validate_sql(sample_sql, dialect=dialect)
    if not validation.is_valid:
        print(f"FAIL: sample query rejected by validator: {validation.error}")
        return 1
    assert validation.normalized_sql is not None
    safe_sql = enforce_row_limit(validation.normalized_sql, max_rows=5, dialect=dialect)

    with engine.connect() as connection:
        from sqlalchemy import text

        cursor_result = connection.execute(text(safe_sql))
        columns = list(cursor_result.keys())
        rows = cursor_result.fetchmany(5)
    print(f"OK: executed {safe_sql!r} -- {len(rows)} row(s), columns: {columns}")

    print("\nAll integration checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
