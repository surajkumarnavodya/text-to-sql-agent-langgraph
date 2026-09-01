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

Security note: sampled values are the sharpest edge of this whole project's
prompt-surface, and the one genuinely attacker-writable point (see
CLAUDE.md / SECURITY.md) -- unlike table/column names (constrained by the
database engine's own identifier rules at CREATE TABLE time), a column's
*data* can contain literally anything anyone with INSERT/UPDATE access ever
wrote, including text crafted to look like a system instruction once it
lands in the prompt (e.g. a product name of `"Bikes\n-- ignore prior
instructions..."`, which -- without sanitization -- would render as a
second, instruction-shaped DDL comment line). `_sample_column` runs every
fetched value through `security.sanitization.normalize_text` (which
collapses embedded newlines/control characters to a single space,
preventing exactly that line-injection) and caps its length before it's
ever stored, let alone rendered into a prompt.

A second, independent risk this module guards against: `table_name`/
`column_name` here come from live schema introspection, not a literal in
this codebase -- ordinarily safe (an engine's own identifier rules
constrain what can even be introspected), but if the connected database
ever contains a maliciously-named table/column (creatable via
quoted-identifier DDL by anyone with CREATE privileges on that schema --
e.g. `CREATE TABLE "Products; DROP TABLE Orders;--"`), naively interpolating
that name into a raw SQL string would be a second-order SQL injection
point. `_sample_column` quotes both identifiers via the connected engine's
own `identifier_preparer` (the same dialect-aware quoting/escaping
SQLAlchemy's own reflection code uses internally) before building the
`SELECT DISTINCT` text, rather than trusting they're already safe to
interpolate bare.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.exc import SQLAlchemyError

from config.sensitive_columns import SensitivityTier, is_restricted, load_sensitive_columns
from db.schema_introspection import TableSchemaInfo, render_ddl
from security.sanitization import normalize_text

logger = logging.getLogger(__name__)


class _CursorResultLike(Protocol):
    def fetchmany(self, n: int) -> Any: ...


class _ConnectionLike(Protocol):
    def __enter__(self) -> _ConnectionLike: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> bool | None: ...
    def execute(self, statement: Any) -> _CursorResultLike: ...


class EngineLike(Protocol):
    """Structural stand-in for `sqlalchemy.Engine`, scoped to exactly what
    `_sample_column`/`attach_sample_values` call (`.dialect.identifier_preparer`
    for quoting, `.connect()` for the actual query). A real `Engine`
    satisfies this structurally, so production call sites need no change;
    the fake engines in `tests/test_value_sampling_injection.py`,
    `tests/test_adversarial_input.py`, and `tests/test_sensitive_columns.py`
    (which use a real `sqlalchemy.engine.default.DefaultDialect` for
    `.dialect`, not a stub) satisfy it too."""

    @property
    def dialect(self) -> Dialect: ...

    def connect(self) -> _ConnectionLike: ...


# Generous cap on one sampled value's length -- in practice values are
# already short (columns longer than _MAX_DECLARED_LENGTH below never
# qualify for sampling at all), so this is a hard safety net, not the
# primary size control.
_MAX_VALUE_LENGTH = 200

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


def _sample_column(engine: EngineLike, table_name: str, column_name: str) -> tuple[str, ...] | None:
    """Returns up to `_MAX_DISTINCT_VALUES` distinct values, or None if too many/failed.

    `table_name`/`column_name` are quoted via the connected engine's own
    `identifier_preparer` before being interpolated into the query text --
    see this module's docstring for why a malicious identifier (not just a
    malicious *value*) is a real, if second-order, risk here.
    """
    quote = engine.dialect.identifier_preparer.quote
    quoted_table = quote(table_name)
    quoted_column = quote(column_name)
    try:
        with engine.connect() as connection:
            cursor_result = connection.execute(
                text(f"SELECT DISTINCT {quoted_column} FROM {quoted_table}")
            )
            rows = cursor_result.fetchmany(_MAX_DISTINCT_VALUES + 1)
    except SQLAlchemyError as exc:
        logger.warning(
            "Value sampling failed for %s.%s, skipping: %s", table_name, column_name, exc
        )
        return None

    if len(rows) > _MAX_DISTINCT_VALUES:
        return None

    # Every non-NULL value is genuinely attacker-writable data (see this
    # module's docstring) -- normalized and length-capped here, at the
    # point it's fetched, before it's ever stored or rendered into a
    # prompt. A value that normalizes to empty (e.g. one made up entirely
    # of control characters) is dropped rather than kept as a blank entry.
    cleaned_values = set()
    for row in rows:
        if row[0] is None:
            cleaned_values.add("NULL")
            continue
        cleaned = normalize_text(str(row[0]).strip())[:_MAX_VALUE_LENGTH]
        if cleaned:
            cleaned_values.add(cleaned)

    values = sorted(cleaned_values, key=lambda v: (v == "NULL", v))
    return tuple(values) if values else None


def attach_sample_values(
    engine: EngineLike,
    tables: list[TableSchemaInfo],
    sensitive_columns: dict[tuple[str, str], SensitivityTier] | None = None,
) -> list[TableSchemaInfo]:
    """Re-renders each table's DDL with real sample values for qualifying columns.

    Qualifying = a string-typed (VARCHAR/CHAR family), non-key, declared-
    length-<=50 column with <=20 distinct values actually present in the
    data. This generically catches short "code" columns (ProductLine,
    Class, Style, Gender, MaritalStatus, BusinessType, ...) and genuinely
    low-cardinality descriptive columns (EnglishProductCategoryName) alike,
    anywhere in the schema -- not a hardcoded list of column names.

    A column classified "restricted" in `config/sensitive_columns.yaml` (see
    `config.sensitive_columns`) is never sampled, regardless of cardinality
    or how well it would otherwise qualify -- this is a hard exclusion, not
    another heuristic, since the 20-distinct-value cardinality cap above is
    a side effect of a check built for a different reason (disambiguating
    coded columns) and provides no protection for a low-cardinality
    sensitive column (e.g. a small, closed set of medical/demographic
    categories) -- see `docs/GOVERNANCE.md`'s "Data classification policy."

    Args:
        engine: A read-only SQLAlchemy engine.
        tables: Already-introspected tables (from `introspect_schema()`).
        sensitive_columns: Optional pre-loaded classification map (mainly
            for tests / callers that already loaded it for another reason).
            Defaults to `config.sensitive_columns.load_sensitive_columns()`.

    Returns:
        A new list of `TableSchemaInfo`, same columns/foreign_keys, with
        `ddl` re-rendered to include sample values where found. Tables/
        columns where sampling fails, doesn't qualify, or is restricted are
        left exactly as `introspect_schema()` produced them.
    """
    classifications = (
        sensitive_columns if sensitive_columns is not None else load_sensitive_columns()
    )
    enriched: list[TableSchemaInfo] = []
    for table in tables:
        sample_values: dict[str, tuple[str, ...]] = {}
        for column in table.columns:
            if not _is_sampling_candidate(column.name, column.type, column.is_primary_key):
                continue
            if is_restricted(table.table_name, column.name, classifications):
                logger.info(
                    "Skipping value sampling for restricted column %s.%s",
                    table.table_name,
                    column.name,
                )
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
