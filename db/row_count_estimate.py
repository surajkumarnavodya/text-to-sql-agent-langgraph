"""Approximate, catalog-only row-count metadata for one table, per `DB_TYPE`.

Prompt 06 (`06_DATABASE_DISCOVERY_CONTRACT.md`)'s "safe row-count metadata"
requirement -- deliberately never a `SELECT COUNT(*)`, which is the one
thing `db/schema_introspection.py`'s own docstring promises introspection
never does (a full-table scan on a large fact table is exactly the
"expensive profiling" this module's own sibling, `db/value_sampling.py`,
is already kept separate from). Every strategy below reads a pre-computed,
already-maintained catalog/statistics view instead:

  - **postgresql**: `pg_stat_user_tables.n_live_tup` -- maintained by
    autovacuum/autoanalyze, approximate.
  - **mysql**: `information_schema.tables.TABLE_ROWS` -- an InnoDB
    estimate, not exact (documented MySQL behavior).
  - **mssql**: `sys.dm_db_partition_stats`, summed across the heap/
    clustered-index partitions (`index_id IN (0, 1)`) -- the same
    mechanism SSMS's own "Row Count" column uses.
  - **oracle**: `ALL_TABLES.NUM_ROWS` -- populated by `DBMS_STATS`
    gathering; `NULL` (surfaced as `None` here) if stats were never
    gathered for that table.

This queries `information_schema`/catalog objects directly via a
hardcoded, trusted, backend-authored SQL string -- never through
`agent.sql_validator.validate_sql`'s LLM-output allowlist, and not in
tension with that validator's own system-catalog denial (`agent
.sql_validator._SYSTEM_CATALOG_SCHEMAS`): that denial exists to stop an
*LLM-generated* query (in response to a business question) from digging
through catalog metadata, not to restrict this application's own trusted
administrative introspection code, exactly the same distinction
`db/connection.py`'s version/write-privilege queries and
`db/schema_introspection.py`'s own `Inspector` calls already rely on.

Fails open on any error, unsupported `db_type`, or a `None`/missing
catalog value -- row-count is a nice-to-have discovery metadata field,
never a reason discovery itself fails. Deliberately simpler than
`db/query_cost.py`'s thread-based timeout wrapper: a catalog-only
single-row lookup against an already-maintained statistics view has no
realistic pathological-hang scenario the way compiling a query *plan*
does, so a plain try/except is the right amount of machinery here, not a
second copy of that thread/force-abort mechanism.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


def _postgresql_row_count(engine: Engine, table_name: str, schema: str | None) -> int | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT n_live_tup FROM pg_stat_user_tables "
                "WHERE relname = :table AND (:schema IS NULL OR schemaname = :schema)"
            ),
            {"table": table_name, "schema": schema},
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _mysql_row_count(engine: Engine, table_name: str, schema: str | None) -> int | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT TABLE_ROWS FROM information_schema.tables "
                "WHERE table_name = :table AND (:schema IS NULL OR table_schema = :schema)"
            ),
            {"table": table_name, "schema": schema},
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _mssql_row_count(engine: Engine, table_name: str, schema: str | None) -> int | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT SUM(p.row_count) FROM sys.dm_db_partition_stats p "
                "JOIN sys.tables t ON p.object_id = t.object_id "
                "JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE t.name = :table AND (:schema IS NULL OR s.name = :schema) "
                "AND p.index_id IN (0, 1)"
            ),
            {"table": table_name, "schema": schema},
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _oracle_row_count(engine: Engine, table_name: str, schema: str | None) -> int | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT NUM_ROWS FROM ALL_TABLES "
                "WHERE TABLE_NAME = :table AND (:schema IS NULL OR OWNER = :schema)"
            ),
            # Oracle's data dictionary stores unquoted identifiers upper-cased.
            {"table": table_name.upper(), "schema": schema.upper() if schema else None},
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


_STRATEGIES = {
    "postgresql": _postgresql_row_count,
    "mysql": _mysql_row_count,
    "mssql": _mssql_row_count,
    "oracle": _oracle_row_count,
}


def supports_row_count_estimate(db_type: str) -> bool:
    """Whether `db_type` has a real catalog-only row-count strategy above."""
    return db_type in _STRATEGIES


def estimate_row_count(
    engine: Engine, table_name: str, schema: str | None, db_type: str
) -> int | None:
    """Approximate row count for one table/view, or `None` if unavailable.

    Args:
        engine: A read-only SQLAlchemy engine for the database `table_name`
            belongs to.
        table_name: The table's (or view's) bare name, as returned by
            `db.schema_introspection.introspect_schema`.
        schema: The configured schema restriction (`DatabaseConnectionConfig
            .db_schema`), or `None` for "whichever schema `Inspector`
            resolved by default."
        db_type: Selects the per-engine strategy above.

    Returns:
        An approximate row count, or `None` if `db_type` has no strategy,
        the table has no row-count statistics yet (e.g. Oracle before
        `DBMS_STATS` has run), or the lookup failed for any reason. Never
        raises -- a failure here is logged at debug level and treated
        exactly like "no row-count metadata available," never a reason
        discovery itself fails.
    """
    strategy = _STRATEGIES.get(db_type)
    if strategy is None:
        return None
    try:
        return strategy(engine, table_name, schema)
    except SQLAlchemyError as exc:
        logger.debug(
            "[row_count_estimate] failed for table=%r db_type=%r: %s", table_name, db_type, exc
        )
        return None
