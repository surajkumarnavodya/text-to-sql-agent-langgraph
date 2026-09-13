"""Best-effort chart auto-selection for a query result.

Used by the `/execute` API endpoint (`api/main.py`), whose Plotly-figure
JSON the React dashboard renders via chart.js
(`frontend/src/lib/chartAdapter.ts`) -- a single source of truth for the
chart-selection heuristic itself, decoupled from how any particular
frontend actually draws it.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


def build_chart(df: pd.DataFrame) -> go.Figure | None:
    """Best-effort bar/line chart from a query result, or None if not chartable.

    Heuristic: needs at least one numeric column and one non-numeric (label)
    column. If the label column looks like a date/time, use a line chart
    (trend over time); otherwise a bar chart (comparison across categories).
    Anything else (all-numeric, all-text, too many columns) is left as a
    table only -- a wrong guess at a chart is worse than no chart.
    """
    if df.empty or len(df.columns) < 2:
        return None

    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    label_cols = [c for c in df.columns if c not in numeric_cols]
    if not numeric_cols or not label_cols:
        return None

    label_col = label_cols[0]
    value_col = numeric_cols[0]

    is_date_like = "date" in label_col.lower() or pd.api.types.is_datetime64_any_dtype(
        df[label_col]
    )
    if is_date_like:
        try:
            plot_df = df.copy()
            plot_df[label_col] = pd.to_datetime(plot_df[label_col])
            plot_df = plot_df.sort_values(label_col)
        except (ValueError, TypeError):
            plot_df = df
        return px.line(plot_df, x=label_col, y=value_col, markers=True)

    # Cap category count so a bar chart doesn't render hundreds of bars.
    plot_df = df.nlargest(30, value_col) if len(df) > 30 else df
    return px.bar(plot_df, x=label_col, y=value_col)
