"""Samples real distinct values for low-cardinality text columns.

Why this exists: a coded column like `DimProduct.ProductLine` (values
`M`/`R`/`S`/`T`/NULL) looks, from its name and type alone, like it could hold
almost anything -- including a business term the LLM half-recognizes, like
"Bikes". Showing the LLM the column's *actual* values up front is a general
defense against exactly that confusion, for any column shaped like this
anywhere in the schema, not a special case for `ProductLine`.

This is deliberately a separate module from `db/schema_introspection.py`:
that module's docstring promises it never touches table data, only catalog
metadata. This module is the one place that breaks that abstraction on
purpose, scoped tightly (read-only, small `SELECT DISTINCT`s, bounded
cardinality, string columns only) and called explicitly at embedding-build
time (`scripts/build_embeddings.py`, `ui/app.py`) -- never on the hot query
path.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from db.schema_introspection import TableSchemaInfo, render_ddl

logger = logging.getLogger(__name__)

# Only these SQLAlchemy-rendered type name families are ever sampled --
# numeric/date/binary columns are never business-meaningful "codes" in the
# way a short string column is, and sampling them would be noise at best.
_STRING_TYPE_RE = re.compile(r"^(N?VARCHAR|N?CHAR)\b", re.IGNORECASE)

# Column name suffixes that are load-bearing for joins, not descriptive
# values -- even if a *Key column were somehow string-typed and low
# cardinality, sampling it would mislead more than help.
_KEY_SUFFIXES = ("key", "id")

# A column with more distinct values than this is either free text or high-
# cardinality (e.g. names, emails) -- exactly the shape we must not sample,
# both because it's not a useful "code list" for the prompt and because
# high-cardinality string columns are the ones most likely to hold PII. This
# cap is what keeps sampling privacy-safe without needing a column-name
# denylist: PII columns are high-cardinality by nature and never pass it.
_MAX_DISTINCT_VALUES = 20

# Columns declared longer than this are descriptive free text (e.g.
# NVARCHAR(400) descriptions), not short codes -- skip regardless of
# cardinality to keep sampled values short and prompt-cheap.
_MAX_DECLARED_LENGTH = 50

_LENGTH_RE = re.compile(r"\((\d+)\)")


def _is_sampling_candidate(column_name: str, column_type: str, is_primary_key: bool) -> bool:
    if is_primary_key or column_name.lower().endswith(_KEY_SUFFIXES):
        return False
    if not _STRING_TYPE_RE.match(column_type):
        return False
    length_match = _LENGTH_RE.search(column_type)
    return not (length_match and int(length_match.group(1)) > _MAX_DECLARED_LENGTH)


def _sample_column(engine: Engine, table_name: str, column_name: str) -> tuple[str, ...] | None:
    """Returns up to `_MAX_DISTINCT_VALUES` distinct values, or None if too many/failed."""
    try:
        with engine.connect() as connection:
            cursor_result = connection.execute(
                text(f"SELECT DISTINCT {column_name} FROM {table_name}")
            )
            rows = cursor_result.fetchmany(_MAX_DISTINCT_VALUES + 1)
    except SQLAlchemyError as exc:
        logger.warning(
            "Value sampling failed for %s.%s, skipping: %s", table_name, column_name, exc
        )
        return None

    if len(rows) > _MAX_DISTINCT_VALUES:
        return None

    values = sorted(
        {("NULL" if row[0] is None else str(row[0]).strip()) for row in rows},
        key=lambda v: (v == "NULL", v),
    )
    return tuple(values) if values else None


def attach_sample_values(engine: Engine, tables: list[TableSchemaInfo]) -> list[TableSchemaInfo]:
    """Re-renders each table's DDL with real sample values for qualifying columns.

    Qualifying = a string-typed (VARCHAR/CHAR family), non-key, declared-
    length-<=50 column with <=20 distinct values actually present in the
    data. This generically catches short "code" columns (ProductLine,
    Class, Style, Gender, MaritalStatus, BusinessType, ...) and genuinely
    low-cardinality descriptive columns (EnglishProductCategoryName) alike,
    anywhere in the schema -- not a hardcoded list of column names.

    Args:
        engine: A read-only SQLAlchemy engine.
        tables: Already-introspected tables (from `introspect_schema()`).

    Returns:
        A new list of `TableSchemaInfo`, same columns/foreign_keys, with
        `ddl` re-rendered to include sample values where found. Tables/
        columns where sampling fails or doesn't qualify are left exactly as
        `introspect_schema()` produced them.
    """
    enriched: list[TableSchemaInfo] = []
    for table in tables:
        sample_values: dict[str, tuple[str, ...]] = {}
        for column in table.columns:
            if not _is_sampling_candidate(column.name, column.type, column.is_primary_key):
                continue
            values = _sample_column(engine, table.table_name, column.name)
            if values:
                sample_values[column.name] = values

        if not sample_values:
            enriched.append(table)
            continue

        logger.info(
            "Sampled values for %s: %s",
            table.table_name,
            {col: len(vals) for col, vals in sample_values.items()},
        )
        new_ddl = render_ddl(
            table.table_name, table.columns, table.foreign_keys, sample_values=sample_values
        )
        enriched.append(
            TableSchemaInfo(
                table_name=table.table_name,
                columns=table.columns,
                foreign_keys=table.foreign_keys,
                ddl=new_ddl,
            )
        )
    return enriched
