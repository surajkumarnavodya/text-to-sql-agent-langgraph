"""Chart-recommendation metadata for a query result -- NOT a rendered chart.

Actual chart rendering is fully client-side
(`frontend/src/lib/chartEngine.ts`) so a user can switch chart types, remap
axes, sort, and apply a Top-N limit without re-running SQL. This module's
only job is:

1. Classify each result column as numeric, date-like, or text
   (`classify_columns`) -- the same dtype checks a previous Plotly-based
   auto-pick heuristic used internally, now exposed directly to the client
   instead of being baked into one fixed chart the backend rendered.
2. Suggest ONE starting chart type + a short, human-readable reason
   (`recommend_chart`).

The frontend never treats this recommendation as proof a chart is valid --
it independently re-validates against the actual returned rows before
enabling any chart type (see that module's own docstring) -- this keeps
the "never treat the LLM/heuristic's suggestion as proof" requirement true
even for a *correct* heuristic, since data can still fail validation for
reasons this module doesn't check (e.g. too many categories for a pie
chart).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Literal, TypedDict

import pandas as pd

ColumnType = Literal["numeric", "date", "text"]

# Mirrors `analytics.engine._NUMERIC_TYPES`/`agent.insight._NUMERIC_TYPES`
# exactly (by value, not by import -- see this codebase's established
# convention for sharing a constant across modules that shouldn't
# cross-import, e.g. `analytics/engine.py`'s own docstring for why) --
# bool is deliberately excluded, same reason: Python's bool is an int
# subclass, and a True/False column is not a numeric measure.
_NUMERIC_SCALAR_TYPES = (int, float, Decimal)


def _is_numeric_object_column(series: pd.Series) -> bool:
    """True when `series` has pandas `object` dtype but every non-null
    value is actually a numeric scalar.

    A real, previously undiagnosed gap: `pd.api.types.is_numeric_dtype`
    is `False` for an `object`-dtype column, and a SQL Server
    `DECIMAL`/`MONEY`/`NUMERIC` column comes back from `pyodbc` as
    Python `decimal.Decimal` -- which pandas stores as `object`, not a
    numeric dtype, regardless of the values themselves being perfectly
    numeric. Without this check, `classify_columns` mislabels every such
    column `"text"`, which silently disabled the entire client-side
    chart-eligibility check (`frontend/src/lib/chartEngine.ts`) for any
    query whose only numeric measure was a money/decimal column -- the
    common case for a real business database (revenue, sales, price,
    salary, ...). An all-null column returns `False` (nothing to call
    numeric); `bool` values are deliberately excluded via
    `_NUMERIC_SCALAR_TYPES`'s own exclusion.
    """
    non_null = series.dropna()
    if non_null.empty:
        return False
    return bool(
        non_null.map(
            lambda v: isinstance(v, _NUMERIC_SCALAR_TYPES) and not isinstance(v, bool)
        ).all()
    )


def _looks_like_date(name: str, series: pd.Series) -> bool:
    """Whether a non-numeric column looks like it holds dates -- by name
    (`"date"`/`"time"` substring) or by successfully parsing a sample of its
    actual values. Never raises; an unparseable sample just means "not a
    date"."""
    if "date" in name.lower() or "time" in name.lower():
        return True
    sample = series.dropna().head(20)
    if sample.empty:
        return False
    try:
        # format="mixed" (per-element inference) is what a heterogeneous
        # sample of date-shaped strings needs -- suppresses pandas' own
        # "could not infer format" warning, which would otherwise fire on
        # every single call for exactly this expected case.
        pd.to_datetime(sample, errors="raise", format="mixed")
        return True
    except (ValueError, TypeError):
        return False


def classify_columns(df: pd.DataFrame) -> dict[str, ColumnType]:
    """Classifies every column in `df` as `"numeric"`, `"date"`, or
    `"text"` -- returned directly to the client (`ExecuteResponse
    .column_types`) so its chart engine doesn't have to re-guess types
    from raw JSON values (a JS `typeof` check can't reliably distinguish a
    date string from an arbitrary string the way pandas' own dtype
    inference plus a parse attempt can).

    `pd.api.types.is_numeric_dtype` alone is not sufficient: a SQL
    Server `DECIMAL`/`MONEY`/`NUMERIC` column arrives via `pyodbc` as
    Python `decimal.Decimal`, which pandas stores as `object` dtype, not
    a numeric one -- `_is_numeric_object_column` is the fallback that
    catches this (see its own docstring for the real, previously
    undiagnosed bug this closes: the client's "Visualize" action was
    unreachable for any query whose only numeric measure was a
    money/decimal column)."""
    types: dict[str, ColumnType] = {}
    for col in df.columns:
        series = df[col]
        if pd.api.types.is_numeric_dtype(series) or _is_numeric_object_column(series):
            types[col] = "numeric"
        elif pd.api.types.is_datetime64_any_dtype(series) or _looks_like_date(col, series):
            types[col] = "date"
        else:
            types[col] = "text"
    return types


class ChartRecommendation(TypedDict):
    """One suggested starting point -- `chart_type` is a plain string here
    (not a closed `Literal`) since the frontend owns the actual list of
    supported chart type ids (`frontend/src/lib/chartEngine.ts`'s
    `ChartTypeId`); this module only ever emits a handful of them
    (`"kpi"`, `"line"`, `"bar"`, `"scatter"`)."""

    chart_type: str
    reason: str
    x_column: str | None
    y_column: str | None


def recommend_chart(
    df: pd.DataFrame, column_types: Mapping[str, str]
) -> ChartRecommendation | None:
    """Suggests one chart type for `df`, or `None` if nothing about this
    shape suggests a chart at all (the frontend still independently checks
    every other type -- a `None` here just means "no confident starting
    point," not "no chart is possible").

    Heuristic, cheapest-shape-first:
    - Exactly one row and exactly one numeric column (with at most one
      other, non-numeric column, e.g. a label) -- a single aggregate value
      reads best as a KPI/stat card, not a one-bar bar chart.
    - A date-like column present alongside a numeric one -- a trend over
      time, so a line chart.
    - A text/category column present alongside a numeric one -- a
      comparison across categories, so a bar chart.
    - Two or more numeric columns and nothing else -- a possible
      relationship between two measures, so a scatter chart.
    - Anything else (all-text, no numeric column at all, empty) -- no
      recommendation; table-only is the honest default.
    """
    if df.empty or len(df.columns) == 0:
        return None

    numeric_cols = [c for c, t in column_types.items() if t == "numeric"]
    date_cols = [c for c, t in column_types.items() if t == "date"]
    text_cols = [c for c, t in column_types.items() if t == "text"]

    if len(df) == 1 and len(numeric_cols) == 1 and len(df.columns) <= 2:
        return ChartRecommendation(
            chart_type="kpi",
            reason="A single value reads most clearly as a stat card.",
            x_column=None,
            y_column=numeric_cols[0],
        )

    if not numeric_cols:
        return None

    if date_cols:
        return ChartRecommendation(
            chart_type="line",
            reason="A date column with a numeric measure usually shows a trend over time.",
            x_column=date_cols[0],
            y_column=numeric_cols[0],
        )

    if text_cols:
        return ChartRecommendation(
            chart_type="bar",
            reason="A category column with a numeric measure compares cleanly as a bar chart.",
            x_column=text_cols[0],
            y_column=numeric_cols[0],
        )

    if len(numeric_cols) >= 2:
        return ChartRecommendation(
            chart_type="scatter",
            reason="Two numeric measures can reveal a relationship as a scatter plot.",
            x_column=numeric_cols[0],
            y_column=numeric_cols[1],
        )

    return None
