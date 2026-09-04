"""Manual end-to-end integration check against your REAL, configured database(s).

Not part of the `pytest` suite (see `tests/` -- everything there is mocked,
no real DB required) and never run by CI. Run this yourself, on demand, when
you want to confirm the full path actually works against your database(s):
connection -> schema introspection -> embeddings -> retrieval -> a real
read-only query execution -- once per configured database (`Settings.
databases` -- a plain single-database `.env` has exactly one, named
"default"; a multi-database `.env` sets `DB_CONNECTIONS=name1,name2,...`).
With two or more databases configured, an extra step demonstrates
auto-routing (`embeddings.retriever.select_database`).

Read-only: this script never writes to any database. The one query it runs
per database (`SELECT * ... LIMIT 5`, plus whatever `test_db_connection.py`
-style checks it reuses) is validated through the same `agent.sql_validator`
allowlist as everything else in the app.

Usage (from repo root, with the venv activated and a real .env configured):

    python scripts\\integration_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.exceptions import SchemaRetrievalError  # noqa: E402
from agent.sql_validator import enforce_row_limit, validate_sql  # noqa: E402
from config.settings import (  # noqa: E402
    ConfigurationError,
    DatabaseConnectionConfig,
    Settings,
    get_settings,
)
from db.connection import get_read_only_engine, get_sqlglot_dialect, test_connection  # noqa: E402
from db.schema_introspection import introspect_schema  # noqa: E402
from embeddings.retriever import retrieve_relevant_schema, select_database  # noqa: E402
from embeddings.schema_indexer import build_index  # noqa: E402


def _step(name: str) -> None:
    print(f"\n--- {name} ---")


def _run_for_connection(settings: Settings, config: DatabaseConnectionConfig) -> str | None:
    """Runs the full single-database pipeline check. Returns a sample table name on success."""
    _step(f"1. Connection ({config.name})")
    result = test_connection(config)
    if not result.success:
        print(f"FAIL: {result.message}")
        return None
    print(f"OK: connected. DB version: {result.db_version or '(unknown)'}")

    _step(f"2. Schema introspection ({config.name})")
    try:
        engine = get_read_only_engine(config)
    except ConfigurationError as exc:
        print(f"FAIL: {exc}")
        return None
    tables = introspect_schema(engine, schema=config.db_schema)
    if not tables:
        print("FAIL: no tables visible. Check DB_SCHEMA and the DB user's privileges.")
        return None
    print(
        f"OK: {len(tables)} table(s) introspected: {', '.join(t.table_name for t in tables[:10])}"
    )

    _step(f"3. Embeddings index ({config.name})")
    count = build_index(tables, config.name, settings=settings)
    print(f"OK: {count} table(s) embedded/cached.")

    _step(f"4. Schema retrieval ({config.name})")
    sample_question = f"Show me some rows from {tables[0].table_name}"
    retrieved = retrieve_relevant_schema(
        sample_question, db_name=config.name, top_k=min(settings.schema_top_k, len(tables))
    )
    if not retrieved:
        print("FAIL: retrieval returned no tables for a sample question.")
        return None
    print(
        f"OK: retrieved {len(retrieved)} table(s) for {sample_question!r}: "
        f"{[t['table_name'] for t in retrieved]}"
    )

    _step(f"5. End-to-end read-only query execution ({config.name})")
    dialect = get_sqlglot_dialect(config.db_type)
    sample_sql = f"SELECT * FROM {tables[0].table_name}"
    validation = validate_sql(sample_sql, dialect=dialect)
    if not validation.is_valid:
        print(f"FAIL: sample query rejected by validator: {validation.error}")
        return None
    assert validation.normalized_sql is not None
    safe_sql = enforce_row_limit(validation.normalized_sql, max_rows=5, dialect=dialect)

    with engine.connect() as connection:
        from sqlalchemy import text

        cursor_result = connection.execute(text(safe_sql))
        columns = list(cursor_result.keys())
        rows = cursor_result.fetchmany(5)
    print(f"OK: executed {safe_sql!r} -- {len(rows)} row(s), columns: {columns}")

    return tables[0].table_name


def main() -> int:
    settings = get_settings()

    all_ok = True
    sample_tables: dict[str, str] = {}
    for index, config in enumerate(settings.databases):
        if index:
            print("\n" + "=" * 60)
        print(f"=== {config.name} ===")
        table_name = _run_for_connection(settings, config)
        if table_name is None:
            all_ok = False
        else:
            sample_tables[config.name] = table_name

    if len(settings.databases) > 1 and len(sample_tables) >= 2:
        _step("6. Multi-database auto-routing")
        for db_name, table_name in sample_tables.items():
            question = f"Show me some rows from {table_name}"
            try:
                selection = select_database(question, settings)
            except SchemaRetrievalError as exc:
                print(f"FAIL: routing failed for {question!r}: {exc}")
                all_ok = False
                continue
            outcome = "OK" if selection.db_name == db_name else "NOTE (ambiguous sample data)"
            print(
                f"{outcome}: {question!r} (expected database {db_name!r}) -> routed to "
                f"{selection.db_name!r} (scores: {selection.scores_by_db})"
            )

    print("\nAll integration checks passed." if all_ok else "\nSome integration checks FAILED.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
