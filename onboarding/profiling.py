"""Data profiling for onboarding -- Prompt 08
(`08_ONBOARDING_ENGINE_CONTRACT.md`): null %, distinct count, uniqueness,
min/max, average, percentiles, value distribution, freshness, duplicate
keys, and orphan relationships.

Deliberately a separate, explicit, opt-in stage from
`db.schema_introspection` (which never touches table data at all) and
from `db.value_sampling`/`db.row_count_estimate` (each scoped to one
narrow, cheap signal) -- profiling genuinely reads real column data and
is not free for a large table, which is exactly why Prompt 06's "separate
metadata discovery from expensive profiling" principle applies here by
being its own onboarding pipeline stage, never run as part of an ordinary
schema refresh.

Two cost tiers, matching what each stat actually requires:
  - **Server-side aggregates** (`profile_column`'s count/distinct/min/max/
    avg) -- one exact aggregate query per column. Accurate, but an
    unavoidable full-column read server-side (there is no approximate way
    to get an exact `COUNT(DISTINCT ...)`), which is why this is its own
    explicit stage, never automatic.
  - **Bounded client-side sampling** (percentiles, top-N value
    distribution, duplicate-key/orphan-relationship detection) -- capped
    via `fetchmany()`, the same established pattern `db.value_sampling
    ._sample_column` already uses. Percentiles here are **approximate**
    (computed from a bounded sample, not a dialect-specific exact
    `PERCENTILE_CONT`) specifically so the same code path works
    identically across all four supported engines -- disclosed, not
    silently assumed exact.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from db.schema_introspection import TableSchemaInfo

logger = logging.getLogger(__name__)

_MAX_SAMPLE_SIZE = 500
_MAX_TOP_VALUES = 10
_MAX_STRING_VALUE_LENGTH = 200

_DATETIME_TYPE_MARKERS = ("DATE", "TIME", "TIMESTAMP")
_NUMERIC_TYPE_MARKERS = ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL", "MONEY")


def _looks_datetime(type_str: str) -> bool:
    upper = type_str.upper()
    return any(marker in upper for marker in _DATETIME_TYPE_MARKERS)


def _looks_numeric(type_str: str) -> bool:
    upper = type_str.upper()
    return any(marker in upper for marker in _NUMERIC_TYPE_MARKERS)


def _quote(engine: Engine, identifier: str) -> str:
    return engine.dialect.identifier_preparer.quote(identifier)


def _qualified(engine: Engine, schema: str | None, table_name: str) -> str:
    if schema:
        return f"{_quote(engine, schema)}.{_quote(engine, table_name)}"
    return _quote(engine, table_name)


def _stringify(value: object) -> str | None:
    if value is None:
        return None
    return str(value)[:_MAX_STRING_VALUE_LENGTH]


@dataclass(frozen=True)
class ColumnProfile:
    """One column's real-data profile. `top_values`/percentiles/
    `freshness_days` are `None`/empty when not applicable to this
    column's type or when the underlying query failed (fails open --
    see `profile_column`'s own docstring)."""

    table_name: str
    column_name: str
    total_rows: int
    null_count: int
    null_fraction: float
    distinct_count: int
    uniqueness_ratio: float
    min_value: str | None
    max_value: str | None
    avg_value: float | None
    percentiles: dict[str, float] | None
    top_values: tuple[tuple[str, int], ...]
    freshness_days: float | None


@dataclass(frozen=True)
class DuplicateKeyFinding:
    """A declared/candidate unique key that, in reality, has duplicate
    values -- a real data-quality issue, not a schema-shape claim."""

    table_name: str
    column_name: str
    duplicate_group_count: int


@dataclass(frozen=True)
class OrphanRelationshipFinding:
    """A relationship (declared or candidate) where a real, bounded
    sample of source values found some with no matching target row."""

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    orphan_fraction: float
    sample_size: int


def profile_column(
    engine: Engine,
    table_name: str,
    column_name: str,
    column_type: str,
    schema: str | None = None,
    sample_size: int = 200,
) -> ColumnProfile | None:
    """Profiles one column: exact count/null/distinct/min/max/avg via one
    server-side aggregate query, plus a bounded-sample pass for
    percentiles (numeric) and top-N value distribution (non-numeric,
    likely-categorical only -- skipped for a column this function's own
    sample shows is high-cardinality, to avoid a useless, huge
    "distribution" of effectively-unique values) and freshness
    (datetime-typed only).

    Returns:
        A `ColumnProfile`, or `None` if the aggregate query itself failed
        (logged, never raised) -- profiling one column must never abort
        profiling the rest of the table.
    """
    bounded_sample_size = min(sample_size, _MAX_SAMPLE_SIZE)
    quoted_column = _quote(engine, column_name)
    qualified_table = _qualified(engine, schema, table_name)
    is_numeric = _looks_numeric(column_type)

    avg_expr = f", AVG({quoted_column}) AS avg_val" if is_numeric else ""
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT COUNT(*) AS total, COUNT({quoted_column}) AS non_null, "  # nosec B608
                    f"COUNT(DISTINCT {quoted_column}) AS distinct_count, "
                    f"MIN({quoted_column}) AS min_val, MAX({quoted_column}) AS max_val"
                    f"{avg_expr} FROM {qualified_table}"
                )
            ).fetchone()
    except SQLAlchemyError as exc:
        logger.debug(
            "[profiling] aggregate query failed for %s.%s: %s", table_name, column_name, exc
        )
        return None
    if row is None:
        return None

    total = int(row.total)
    non_null = int(row.non_null)
    null_count = total - non_null
    distinct_count = int(row.distinct_count)

    percentiles = None
    top_values: tuple[tuple[str, int], ...] = ()
    freshness_days = None
    if is_numeric:
        percentiles = _sample_percentiles(
            engine, qualified_table, quoted_column, bounded_sample_size
        )
    elif distinct_count > 0 and distinct_count <= bounded_sample_size:
        # Only computed for columns that already look low-cardinality
        # (per the exact distinct_count above) -- a genuinely high-
        # cardinality column's "top values" would just be a list of
        # effectively-unique values, not a useful distribution.
        top_values = _sample_top_values(engine, qualified_table, quoted_column, bounded_sample_size)
    if _looks_datetime(column_type):
        freshness_days = _sample_freshness_days(engine, qualified_table, quoted_column)

    return ColumnProfile(
        table_name=table_name,
        column_name=column_name,
        total_rows=total,
        null_count=null_count,
        null_fraction=round(null_count / total, 4) if total else 0.0,
        distinct_count=distinct_count,
        uniqueness_ratio=round(distinct_count / non_null, 4) if non_null else 0.0,
        min_value=_stringify(row.min_val),
        max_value=_stringify(row.max_val),
        avg_value=float(row.avg_val) if is_numeric and row.avg_val is not None else None,
        percentiles=percentiles,
        top_values=top_values,
        freshness_days=freshness_days,
    )


def _sample_percentiles(
    engine: Engine, qualified_table: str, quoted_column: str, sample_size: int
) -> dict[str, float] | None:
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT {quoted_column} FROM {qualified_table} "  # nosec B608
                    f"WHERE {quoted_column} IS NOT NULL"
                )
            ).fetchmany(sample_size)
    except SQLAlchemyError as exc:
        logger.debug("[profiling] percentile sample failed: %s", exc)
        return None
    values = sorted(float(row[0]) for row in rows if row[0] is not None)
    if not values:
        return None
    return {
        "p50": statistics.median(values),
        "p90": values[min(int(len(values) * 0.9), len(values) - 1)],
        "p99": values[min(int(len(values) * 0.99), len(values) - 1)],
    }


def _sample_top_values(
    engine: Engine, qualified_table: str, quoted_column: str, sample_size: int
) -> tuple[tuple[str, int], ...]:
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"SELECT {quoted_column}, COUNT(*) AS n FROM {qualified_table} "  # nosec B608
                    f"WHERE {quoted_column} IS NOT NULL GROUP BY {quoted_column} "
                    f"ORDER BY COUNT(*) DESC"
                )
            ).fetchmany(min(sample_size, _MAX_TOP_VALUES))
    except SQLAlchemyError as exc:
        logger.debug("[profiling] top-values sample failed: %s", exc)
        return ()
    return tuple((_stringify(row[0]) or "", int(row[1])) for row in rows)


def _sample_freshness_days(
    engine: Engine, qualified_table: str, quoted_column: str
) -> float | None:
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    f"SELECT MAX({quoted_column}) AS most_recent FROM {qualified_table}"
                )  # nosec B608
            ).fetchone()
    except SQLAlchemyError as exc:
        logger.debug("[profiling] freshness query failed: %s", exc)
        return None
    if row is None or row.most_recent is None:
        return None
    most_recent = row.most_recent
    if isinstance(most_recent, datetime):
        if most_recent.tzinfo is None:
            most_recent = most_recent.replace(tzinfo=UTC)
        return round((datetime.now(UTC) - most_recent).total_seconds() / 86400, 2)
    return None


def find_duplicate_keys(
    engine: Engine, tables: list[TableSchemaInfo], schema: str | None = None, sample_size: int = 200
) -> list[DuplicateKeyFinding]:
    """Checks every declared single-column PK/unique constraint for real
    duplicate values -- a data-quality issue a schema declaration alone
    can't reveal (a unique *constraint* that was added after duplicate
    rows already existed, or added without validating existing data on a
    dialect that allows it, is a real, if rare, situation worth
    surfacing). Bounded via `fetchmany()`; fails open per-table."""
    bounded_sample_size = min(sample_size, _MAX_SAMPLE_SIZE)
    findings: list[DuplicateKeyFinding] = []
    for table in tables:
        if table.is_view:
            continue
        candidate_columns: set[str] = {c.name for c in table.columns if c.is_primary_key}
        for constraint in table.unique_constraints:
            if len(constraint) == 1:
                candidate_columns.add(constraint[0])
        for column_name in sorted(candidate_columns):
            try:
                qualified_table = _qualified(engine, schema, table.table_name)
                quoted_column = _quote(engine, column_name)
                with engine.connect() as connection:
                    rows = connection.execute(
                        text(
                            f"SELECT {quoted_column} FROM {qualified_table} "  # nosec B608
                            f"GROUP BY {quoted_column} HAVING COUNT(*) > 1"
                        )
                    ).fetchmany(bounded_sample_size)
            except SQLAlchemyError as exc:
                logger.debug(
                    "[profiling] duplicate-key check failed for %s.%s: %s",
                    table.table_name,
                    column_name,
                    exc,
                )
                continue
            if rows:
                findings.append(
                    DuplicateKeyFinding(
                        table_name=table.table_name,
                        column_name=column_name,
                        duplicate_group_count=len(rows),
                    )
                )
    return findings


def find_orphan_rows(
    engine: Engine,
    source_table: str,
    source_column: str,
    target_table: str,
    target_column: str,
    schema: str | None = None,
    sample_size: int = 200,
) -> OrphanRelationshipFinding | None:
    """Bounded check for source values with no matching target row, for
    one declared or candidate relationship. Returns `None` (not a
    zero-orphan finding) if the query failed or there were no non-NULL
    source values to check at all -- callers should treat `None` as "not
    measured," not "confirmed clean."
    """
    bounded_sample_size = min(sample_size, _MAX_SAMPLE_SIZE)
    quoted_source_column = _quote(engine, source_column)
    source_qualified = _qualified(engine, schema, source_table)
    try:
        with engine.connect() as connection:
            sample_rows = connection.execute(
                text(
                    f"SELECT DISTINCT {quoted_source_column} FROM {source_qualified} "  # nosec B608
                    f"WHERE {quoted_source_column} IS NOT NULL"
                )
            ).fetchmany(bounded_sample_size)
    except SQLAlchemyError as exc:
        logger.debug("[profiling] orphan-check source sample failed: %s", exc)
        return None
    sampled_values = [row[0] for row in sample_rows]
    if not sampled_values:
        return None

    quoted_target_column = _quote(engine, target_column)
    target_qualified = _qualified(engine, schema, target_table)
    placeholders = {f"v{i}": value for i, value in enumerate(sampled_values)}
    in_clause = ", ".join(f":{name}" for name in placeholders)
    try:
        with engine.connect() as connection:
            found_rows = connection.execute(
                text(
                    f"SELECT DISTINCT {quoted_target_column} FROM {target_qualified} "  # nosec B608
                    f"WHERE {quoted_target_column} IN ({in_clause})"
                ),
                placeholders,
            ).fetchmany(len(sampled_values))
    except SQLAlchemyError as exc:
        logger.debug("[profiling] orphan-check target lookup failed: %s", exc)
        return None

    found = {row[0] for row in found_rows}
    orphan_count = sum(1 for value in sampled_values if value not in found)
    return OrphanRelationshipFinding(
        source_table=source_table,
        source_column=source_column,
        target_table=target_table,
        target_column=target_column,
        orphan_fraction=round(orphan_count / len(sampled_values), 4),
        sample_size=len(sampled_values),
    )
