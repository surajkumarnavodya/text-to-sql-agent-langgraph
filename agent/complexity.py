"""Detects question-text signals that predict a harder-than-usual SQL generation.

A cheap, regex-only heuristic over the raw question text (mirrors
`agent.followup.classify_followup`'s own approach) run once per question, not
per retry -- negligible next to the real per-attempt costs (an LLM call, a DB
round trip). Used by `agent.graph.run_agent` to widen the retry budget for
questions that are statistically more likely to need more than one or two
self-correction cycles: "top N per group" phrasing, period-over-period
comparisons, and multi-metric asks are exactly the shapes that pushed a real
run to exhaust `MAX_RETRIES` (nested aggregates, wrong-shape TOP N + GROUP BY
-- see `agent.sql_validator._find_nested_aggregate` and
`agent.llm_client._QUERY_PATTERNS_BLOCK` for the corresponding prompt/
validator-side fixes). This module only decides *how many* attempts a
question gets; it has no opinion on how each attempt is generated or judged.
"""

from __future__ import annotations

import re

# "top 5", "bottom 3", "best 10", "highest 3", "lowest 5", etc.
_TOP_N_PATTERN = re.compile(r"\b(top|bottom|best|highest|lowest)\s+\d+\b", re.IGNORECASE)

# Paired with _TOP_N_PATTERN to detect "top N *per group*" specifically
# (e.g. "top 3 products per region", "top 5 for each year") -- a bare
# "top 10 customers" (or "top 10 customers *by* revenue", ordered by a
# metric, no partitioning at all) has no per-group ranking to get wrong, so
# it's not flagged on its own. Deliberately excludes "by": "top N by
# <metric>" is the single most common ordinary top-N phrasing and would
# otherwise false-positive on nearly every one of them.
_PER_GROUP_PATTERN = re.compile(r"\b(each|every|per)\b", re.IGNORECASE)

_GROWTH_PATTERN = re.compile(
    r"\b(growth|year-over-year|yoy|month-over-month|mom|quarter-over-quarter|qoq|"
    r"compared to|vs\.?|trend|change (from|since))\b",
    re.IGNORECASE,
)

_RANKING_WINDOW_PATTERN = re.compile(
    r"\b(rank(ed|ing)?|running total|cumulative|moving average|percentile)\b",
    re.IGNORECASE,
)

# Plain substring keywords (not word-boundary regex) for counting how many
# distinct metrics a question asks for at once -- e.g. the failure this
# module was written after asked for "revenue, profit, distinct customers,
# and year-over-year growth" in one question, four metrics stacked together.
_METRIC_KEYWORDS = (
    "revenue",
    "profit",
    "cost",
    "count",
    "average",
    "total",
    "distinct",
    "margin",
    "ratio",
    "share",
)
_MIN_METRIC_HITS_FOR_SIGNAL = 3


def detect_complexity_signals(question: str) -> list[str]:
    """Returns the distinct complexity signals matched in `question`.

    Args:
        question: The user's natural-language question (already sanitized/
            normalized by `agent.input_guard.check_input`, though this
            function doesn't depend on that).

    Returns:
        A list of matched signal names (possibly empty), from:
        "top_n_per_group", "growth_comparison", "ranking_window",
        "multi_metric". Order is fixed (not meaningful beyond that), so log
        output and tests can compare it directly.
    """
    signals: list[str] = []
    if _TOP_N_PATTERN.search(question) and _PER_GROUP_PATTERN.search(question):
        signals.append("top_n_per_group")
    if _GROWTH_PATTERN.search(question):
        signals.append("growth_comparison")
    if _RANKING_WINDOW_PATTERN.search(question):
        signals.append("ranking_window")
    metric_hits = sum(1 for keyword in _METRIC_KEYWORDS if keyword in question.lower())
    if metric_hits >= _MIN_METRIC_HITS_FOR_SIGNAL:
        signals.append("multi_metric")
    return signals


def compute_max_retries(
    question: str, base_max_retries: int, max_bonus: int
) -> tuple[int, list[str]]:
    """Computes this question's effective retry budget.

    One extra retry per distinct signal matched (`detect_complexity_signals`),
    capped at `max_bonus` -- a question matching every signal still can't
    blow the budget out arbitrarily far, just up to `base_max_retries +
    max_bonus`.

    Args:
        question: The user's natural-language question.
        base_max_retries: `Settings.max_retries`, the pre-existing global cap.
        max_bonus: `Settings.complex_query_max_retry_bonus`. 0 makes this a
            no-op (every question gets exactly `base_max_retries`, unchanged
            from before this module existed).

    Returns:
        `(effective_max_retries, matched_signals)`.
    """
    signals = detect_complexity_signals(question)
    bonus = min(len(signals), max_bonus)
    return base_max_retries + bonus, signals
