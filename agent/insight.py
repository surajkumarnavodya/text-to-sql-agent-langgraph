"""Grounds and shapes the "plain-English insight" narrative feature.

Everything here is pure/deterministic (no LLM, no I/O) -- the only thing
that touches Ollama is `agent.llm_client.generate_insight_from_llm`, which
consumes a `ResultSummary` built here. Keeping the summarization and
grounding logic separate from the LLM call means both `generate_insight_node`
(runtime) and `scripts/run_eval.py` (offline eval) can share the exact same
definition of "what counts as grounded" -- one definition, not two that
could quietly drift apart.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from decimal import Decimal

from security.sanitization import normalize_text

# Types a driver might hand back for a numeric column. Deliberately excludes
# bool -- Python's bool is a subclass of int, and a True/False column is not
# a quantity to compute min/max/sum over.
_NUMERIC_TYPES = (int, float, Decimal)

# Generous cap on the rendered length of a "top label" pulled from actual
# result row data (see summarize_result) before it's ever concatenated into
# the insight prompt -- mirrors db/value_sampling.py's _MAX_VALUE_LENGTH for
# the same reason: this is genuinely attacker-writable data, not metadata.
_MAX_LABEL_LENGTH = 200


def _is_numeric(value: object) -> bool:
    return isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool)


@dataclass(frozen=True)
class ColumnStat:
    """Aggregate shape of one column -- never the column's raw values.

    Attributes:
        name: Column name.
        is_numeric: Whether every value in this column was numeric.
        minimum / maximum / total: Only set when `is_numeric` is True.
        distinct_count: Only set when `is_numeric` is False -- how many
            distinct values the column took across the result.
    """

    name: str
    is_numeric: bool
    minimum: float | None = None
    maximum: float | None = None
    total: float | None = None
    distinct_count: int | None = None


@dataclass(frozen=True)
class ResultSummary:
    """A SMALL, aggregate-only summary of a query result -- what the LLM sees.

    Deliberately excludes raw rows: row count, column names, and per-column
    aggregate stats are all that's included, so prompt size is independent
    of how many rows the query actually returned (CLAUDE.md's "keep the
    result summary passed to the LLM small" constraint) -- a 3-row result
    and a 900-row result produce a summary of roughly the same size.

    `top_label` / `top_value` / `top_share_percent` capture the one
    cross-column relationship worth naming explicitly: for the first
    numeric column paired with the first non-numeric ("label") column,
    which label had the largest total and what share of the column's grand
    total that represents (e.g. "Bikes", 2_300_000.0, 60.4). This is the
    single most common shape of question this app answers ("X by category"),
    and computing the share here -- in Python, exactly -- means the LLM
    never has to do arithmetic to produce a claim like "roughly 60% of the
    total," which it would otherwise be prone to getting wrong.
    """

    row_count: int
    columns: tuple[str, ...]
    column_stats: tuple[ColumnStat, ...]
    top_label_column: str | None = None
    top_label: str | None = None
    top_value_column: str | None = None
    top_value: float | None = None
    top_share_percent: float | None = None

    def allowed_values(self) -> set[float]:
        """Every plain (non-percent) number the insight is allowed to state.

        Used both by this module's own runtime grounding gate
        (`is_insight_grounded`) and by `scripts/run_eval.py`'s eval-set
        grounding checks.
        """
        numbers = {float(self.row_count)}
        for stat in self.column_stats:
            for value in (stat.minimum, stat.maximum, stat.total, stat.distinct_count):
                if value is not None:
                    numbers.add(round(float(value), 2))
        if self.top_value is not None:
            numbers.add(round(float(self.top_value), 2))
        if self.top_label is not None:
            # The "label" can itself be numeric (e.g. a year in a
            # numeric-vs-numeric result -- see summarize_result's fallback),
            # in which case a claim like "2013 was the highest" should be
            # groundable too.
            with contextlib.suppress(ValueError):
                numbers.add(round(float(self.top_label), 2))
        return numbers

    def allowed_percents(self) -> set[float]:
        """Every percentage the insight is allowed to state."""
        return {round(self.top_share_percent, 1)} if self.top_share_percent is not None else set()


def should_skip_insight(columns: list[str], rows: list[tuple]) -> bool:
    """True when an insight would add no value, or there's nothing to summarize.

    Covers two cases: an empty result (nothing to narrate -- no aggregates
    are even computable) and a single-row, single-column result (e.g. "how
    many customers are there?"), where the one returned value already *is*
    the full answer and restating it in a sentence would just be redundant.
    """
    if not rows:
        return True
    return len(rows) == 1 and len(columns) == 1


def summarize_result(columns: list[str], rows: list[tuple]) -> ResultSummary:
    """Reduces a full result set to the small `ResultSummary` sent to the LLM.

    Args:
        columns: Column names, in result order.
        rows: Result rows. Only ever read here -- never forwarded whole to
            the LLM (see `ResultSummary`'s docstring).
    """
    row_count = len(rows)
    numeric_col_indices = [
        idx for idx in range(len(columns)) if rows and all(_is_numeric(row[idx]) for row in rows)
    ]
    label_col_indices = [idx for idx in range(len(columns)) if idx not in numeric_col_indices]

    column_stats: list[ColumnStat] = []
    for idx, name in enumerate(columns):
        values = [row[idx] for row in rows]
        if idx in numeric_col_indices:
            nums = [float(v) for v in values]
            column_stats.append(
                ColumnStat(
                    name=name,
                    is_numeric=True,
                    minimum=min(nums),
                    maximum=max(nums),
                    total=sum(nums),
                )
            )
        else:
            column_stats.append(
                ColumnStat(name=name, is_numeric=False, distinct_count=len(set(values)))
            )

    top_label_column = top_label = top_value_column = None
    top_value = top_share_percent = None
    # The *last* numeric column, not the first: a GROUP BY dimension
    # conventionally comes before its aggregate in a SELECT list (e.g.
    # "SELECT CalendarYear, SUM(SalesAmount)"), so when a dimension is
    # itself numeric (a year), picking the first numeric column as the
    # "value" would misattribute the metric to the wrong column.
    value_idx = numeric_col_indices[-1] if numeric_col_indices else None
    label_idx = None
    if value_idx is not None:
        if label_col_indices:
            label_idx = label_col_indices[0]
        else:
            # No non-numeric column at all (e.g. "sales by year" -- both
            # columns are numeric). Fall back to the first other column as
            # the "label" regardless of its type, so a claim like "2013 was
            # the highest year" is still groundable -- without this, a
            # numeric-vs-numeric result could never support a top/share
            # claim at all, even though the relationship is just as real.
            label_idx = next((i for i in range(len(columns)) if i != value_idx), None)
    if value_idx is not None and label_idx is not None:
        totals_by_label: dict[str, float] = {}
        for row in rows:
            # Row *data*, not schema metadata -- genuinely attacker-writable
            # the same way a sampled schema value is (see db/value_sampling.py),
            # and this is the one point in the insight feature where it flows
            # into a prompt (`agent.llm_client._build_insight_prompt`'s
            # top_label rendering) unless normalized here first.
            label = normalize_text(str(row[label_idx]))[:_MAX_LABEL_LENGTH]
            totals_by_label[label] = totals_by_label.get(label, 0.0) + float(row[value_idx])
        top_label, top_value = max(totals_by_label.items(), key=lambda item: item[1])
        grand_total = sum(totals_by_label.values())
        top_label_column = columns[label_idx]
        top_value_column = columns[value_idx]
        if grand_total:
            top_share_percent = round(100 * top_value / grand_total, 1)

    return ResultSummary(
        row_count=row_count,
        columns=tuple(columns),
        column_stats=tuple(column_stats),
        top_label_column=top_label_column,
        top_label=top_label,
        top_value_column=top_value_column,
        top_value=top_value,
        top_share_percent=top_share_percent,
    )


# Matches a plain number, optionally $-prefixed and comma-grouped, with an
# optional trailing %/K/M/B suffix. Used only for grounding-checking the
# LLM's *output* text -- generation is separately instructed not to
# abbreviate, so this is a defensive parser, not the primary contract.
#
# The comma-grouped branch requires at least one ",\d{3}" group (`+`, not
# `*`): with `*` it can match on a plain, comma-less number too (e.g.
# "2300000.0"), consuming only the first 1-3 digits as a truncated false
# match ("230") and leaving the rest ("0000.0") to be picked up as a second,
# spurious number -- silently corrupting every large ungrouped number.
_NUMBER_RE = re.compile(
    r"\$?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<suffix>%|[kKmMbB](?![a-zA-Z]))?"
)
_SUFFIX_MULTIPLIERS = {"k": 1e3, "m": 1e6, "b": 1e9}


def extract_numbers(text: str) -> list[tuple[float, bool]]:
    """Extracts every number-looking token from `text` as (value, is_percent).

    K/M/B suffixes are expanded to their full value (e.g. "2.3M" -> 2_300_000.0);
    a trailing "%" is flagged separately since percentages and plain
    quantities are never interchangeable when checking groundedness.
    """
    results = []
    for match in _NUMBER_RE.finditer(text):
        raw = match.group("num")
        value = float(raw.replace(",", ""))
        suffix = (match.group("suffix") or "").lower()
        is_percent = suffix == "%"
        if suffix in _SUFFIX_MULTIPLIERS:
            value *= _SUFFIX_MULTIPLIERS[suffix]
        results.append((value, is_percent))
    return results


def _isclose(a: float, b: float, *, abs_tol: float) -> bool:
    return abs(a - b) <= max(abs_tol, abs(b) * 0.01)


def is_insight_grounded(
    insight_text: str, summary: ResultSummary, question: str = "", sql: str = ""
) -> bool:
    """True if every number in `insight_text` is supported by the result data.

    "Supported" means it matches (within a small rounding tolerance) either
    a number derivable from `summary` (row count, a column's min/max/sum/
    distinct count, the top value, or its share as a percentage) or a
    number that literally appears in the question or SQL -- a filter value
    like a year the user themselves typed is not a fabricated claim, even
    though it isn't part of the result data itself.

    This is deliberately a *basic* check (see CLAUDE.md's testing section):
    it catches invented statistics, not subtler issues like a technically-
    present-but-misleading number. Used both as this module's own runtime
    safety net (an insight that fails this is dropped, never shown -- see
    `agent.nodes.generate_insight_node`) and by `scripts/run_eval.py`'s
    eval-set checks.
    """
    allowed_values = summary.allowed_values()
    allowed_percents = summary.allowed_percents()
    for value, _is_percent in extract_numbers(f"{question} {sql}"):
        allowed_values.add(round(value, 2))

    for value, is_percent in extract_numbers(insight_text):
        pool = allowed_percents if is_percent else allowed_values
        if not any(
            _isclose(value, allowed, abs_tol=1.0 if is_percent else 0.5) for allowed in pool
        ):
            return False
    return True
