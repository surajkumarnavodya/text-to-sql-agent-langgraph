"""Rendering-time transforms for the Streamlit results table: human-readable
column labels and hiding surrogate key columns by default.

Pure functions, no Streamlit imports -- same separation as
`agent/sql_validator.py` (logic lives here, wiring lives in `ui/app.py`).
Nothing here touches the SQL that runs, the DataFrame's real columns, or any
agent/db/validator code -- nothing produced here is fed back into a query;
it only decides what gets *displayed* and how it's *labeled*.
"""

from __future__ import annotations

import re

from config.column_labels import ABBREVIATION_EXPANSIONS, SURROGATE_KEY_SUFFIXES
from db.schema_introspection import TableSchemaInfo

# Inserts a space at camelCase/PascalCase word boundaries (before an
# uppercase-then-lowercase run, and between a lowercase/digit and an
# uppercase letter) -- applied per underscore-split piece so "CustName" and
# "product_category" both tokenize the same way.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z][a-z])|(?<=[a-z0-9])(?=[A-Z])")

_ABBREVIATIONS_LOWER: dict[str, str] = {k.lower(): v for k, v in ABBREVIATION_EXPANSIONS.items()}


def _tokenize(raw_name: str) -> list[str]:
    """Splits a raw column name into word tokens (snake_case + camelCase)."""
    tokens: list[str] = []
    for piece in raw_name.split("_"):
        if not piece:
            continue
        tokens.extend(t for t in _CAMEL_BOUNDARY_RE.sub(" ", piece).split() if t)
    return tokens


def format_column_label(raw_name: str) -> str:
    """Converts a raw DB column name into a human-readable display label.

    Splits on PascalCase/camelCase/snake_case boundaries, expands any token
    matching `config.column_labels.ABBREVIATION_EXPANSIONS` (case-
    insensitive), and title-cases the rest. Falls back to a basic space-
    separated title-case of the original name if tokenization yields
    nothing usable, rather than guessing (per the "don't leave the raw name
    or guess incorrectly" requirement).

    Examples:
        "CustName" -> "Customer Name"
        "product_category" -> "Product Category"
        "TotalAmt" -> "Total Amount"

    Args:
        raw_name: The raw column name as it appears in the query result.

    Returns:
        A human-readable label. Never empty if `raw_name` is non-empty.
    """
    tokens = _tokenize(raw_name)
    if not tokens:
        return raw_name.replace("_", " ").title()

    words = [_ABBREVIATIONS_LOWER.get(token.lower(), token.title()) for token in tokens]
    return " ".join(words)


def get_key_column_names(tables: list[TableSchemaInfo]) -> set[str]:
    """Collects every column name (lowercased) known to be a PK or FK.

    Source of truth is live schema introspection (`db.schema_introspection.
    TableSchemaInfo`, already held by `ui/app.py`'s `discovered_tables`) --
    not name-pattern guessing. See `is_probable_surrogate_key` for how this
    combines with the name-pattern fallback for columns that don't trace
    back to a real schema column (e.g. computed/aliased result columns).

    Args:
        tables: Introspected tables, e.g. `ui.app.discovered_tables`.

    Returns:
        Lowercased column names that are a primary key or a foreign key's
        constrained column in at least one table.
    """
    key_columns: set[str] = set()
    for table in tables:
        for column in table.columns:
            if column.is_primary_key:
                key_columns.add(column.name.lower())
        for fk in table.foreign_keys:
            key_columns.update(name.lower() for name in fk.constrained_columns)
    return key_columns


def is_probable_surrogate_key(
    column_name: str,
    key_columns: set[str],
    suffixes: tuple[str, ...] = SURROGATE_KEY_SUFFIXES,
) -> bool:
    """Decides whether a result column is a surrogate key with no business meaning.

    Two independent signals, either sufficient on its own:
      - `column_name` (case-insensitive) is a real schema PK/FK column, per
        `get_key_column_names` -- the primary, schema-backed signal.
      - The column name's *last word token* (same tokenizer as
        `format_column_label`), lowercased, exactly equals one of
        `suffixes` -- a fallback for computed/aliased columns that don't
        trace back to a real schema column name.

    Matching on the last *token* rather than a raw substring/`str.endswith`
    check is deliberate: `"Valid".endswith("id")` is `True`, but `"Valid"`
    is one token and isn't literally "id", so it's correctly left alone.
    Likewise `PostalCode`, `AccountNumber`, `ZipCode` end in `Code`/`Number`,
    not `Id`/`Key`, so genuine business columns that merely look ID-like
    aren't swept up by the pattern fallback.

    Args:
        column_name: The result column's name, as returned by the query.
        key_columns: Output of `get_key_column_names`.
        suffixes: Last-token suffixes (lowercased) that mark a surrogate key.

    Returns:
        True if this column should be hidden from the default results view.
    """
    if column_name.lower() in key_columns:
        return True

    tokens = _tokenize(column_name)
    if not tokens:
        return False
    return tokens[-1].lower() in suffixes


def get_display_columns(columns: list[str], key_columns: set[str]) -> tuple[list[str], bool]:
    """Filters surrogate key columns out of the default results view.

    Args:
        columns: All column names in the query result, in order.
        key_columns: Output of `get_key_column_names`.

    Returns:
        A `(display_columns, used_fallback)` tuple. `used_fallback` is True
        when filtering would have hidden every column (e.g. a query that
        only returned key columns) -- in that case `display_columns` is the
        original, unfiltered list, so the table is never rendered empty.
    """
    filtered = [c for c in columns if not is_probable_surrogate_key(c, key_columns)]
    if not filtered:
        return columns, True
    return filtered, False
