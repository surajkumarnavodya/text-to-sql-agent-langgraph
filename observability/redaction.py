"""Turns a real query result into a log-safe summary -- shape only, never content.

Cell values are never included at any redaction level; that's not a
configurable choice, it's the same "log shape, not content" discipline
`agent/nodes.py::execute_sql_node`'s own comment already states as this
codebase's convention (see CLAUDE.md's coding standards: "Never log ... full
result rows"). What `Settings.log_redaction_level` actually controls is
whether *column names* are included -- a column name can itself be
sensitive in some schemas (`ssn`, `salary`, `date_of_birth`) even though no
row data is logged, so "standard" (the default) includes them and "strict"
drops them too, leaving only counts.

This is deliberately one small, reusable function rather than a per-call-
site judgment call about what's safe to log -- every place in `api`/
`services`/`observability` that wants to log a result set's shape should
call this instead of hand-rolling its own summary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from config.settings import Settings


class ResultLogSummary(TypedDict):
    """What's safe to attach to a log record for an executed query's result.

    `columns` is `None` when the redaction level omits it (`"strict"`) or
    when there is no result to summarize -- always `None` in the latter
    case regardless of level, since there's nothing to redact either way.
    """

    row_count: int
    column_count: int
    columns: list[str] | None


def summarize_result_for_log(
    columns: list[str] | None,
    rows: list[tuple] | None,
    settings: Settings | None = None,
) -> ResultLogSummary:
    """Builds a `ResultLogSummary` for `columns`/`rows` -- never includes cell values.

    Args:
        columns: The result's column names, e.g. `AgentState["result_columns"]`.
        rows: The result's rows, e.g. `AgentState["result_rows"]` -- only
            `len(rows)` is ever read; the row contents themselves are not
            inspected.
        settings: Settings to read `log_redaction_level` from. `None` (e.g.
            no `Settings` in scope) is treated as `"standard"`, the default.

    Returns:
        A `ResultLogSummary` safe to pass to `logger.info("...", extra=...)`
        or embed in a `RequestTrace`.
    """
    redaction_level = settings.log_redaction_level if settings is not None else "standard"
    column_names = list(columns) if columns else []
    include_column_names = redaction_level != "strict" and columns is not None
    return ResultLogSummary(
        row_count=len(rows) if rows else 0,
        column_count=len(column_names),
        columns=column_names if include_column_names else None,
    )
