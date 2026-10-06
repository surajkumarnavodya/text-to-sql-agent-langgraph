"""Bounded-iteration budget and deterministic stop rules for the AI Data Analyst.

Every limit the analyst enforces lives here as a pure function over plain
dict counters, never as an LLM judgment. The graph (`agent.analyst.graph`)
calls `can_afford` before each charged action and `spend` after it, so the
exact same rule set is what production runs and what the tests exercise.

Counters live in `AnalystState["usage"]` (a plain dict, serializable like
every other field on that state) rather than on a mutable tracker object,
which keeps each node a pure function of its input state.

Stop-reason precedence is fixed: timeout, then steps, then subqueries, then
LLM calls, then follow-ups. A deadline always wins, so a slow analysis can
never keep running just because a counter still has headroom.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

StopReason = Literal[
    "timeout",
    "step_budget_exhausted",
    "subquery_budget_exhausted",
    "llm_budget_exhausted",
    "followup_budget_exhausted",
]

ChargeKind = Literal["step", "subquery", "llm_call", "followup"]

_COUNTER_FOR_KIND: dict[ChargeKind, str] = {
    "step": "steps",
    "subquery": "subqueries",
    "llm_call": "llm_calls",
    "followup": "followups",
}


@dataclass(frozen=True)
class AnalystBudget:
    """The hard limits one analysis may not exceed.

    Built once per request from `Settings` (see `budget_from_settings`) and
    stored on state as a plain dict via `to_dict`, so nodes never reach back
    into settings to decide whether they may continue.
    """

    max_steps: int
    max_subqueries: int
    max_llm_calls: int
    max_followups: int
    timeout_seconds: float

    def to_dict(self) -> dict:
        return {
            "max_steps": self.max_steps,
            "max_subqueries": self.max_subqueries,
            "max_llm_calls": self.max_llm_calls,
            "max_followups": self.max_followups,
            "timeout_seconds": self.timeout_seconds,
        }


def budget_from_dict(data: dict) -> AnalystBudget:
    """Inverse of `AnalystBudget.to_dict`, for nodes reading state back."""
    return AnalystBudget(
        max_steps=int(data["max_steps"]),
        max_subqueries=int(data["max_subqueries"]),
        max_llm_calls=int(data["max_llm_calls"]),
        max_followups=int(data["max_followups"]),
        timeout_seconds=float(data["timeout_seconds"]),
    )


def new_usage(started_at: float) -> dict:
    """A fresh counter dict, with the monotonic start time the deadline is
    measured from."""
    return {
        "started_at": started_at,
        "steps": 0,
        "subqueries": 0,
        "llm_calls": 0,
        "followups": 0,
    }


def _limit_for(budget: AnalystBudget, kind: ChargeKind) -> int:
    return {
        "step": budget.max_steps,
        "subquery": budget.max_subqueries,
        "llm_call": budget.max_llm_calls,
        "followup": budget.max_followups,
    }[kind]


_EXHAUSTED_REASON: dict[ChargeKind, StopReason] = {
    "step": "step_budget_exhausted",
    "subquery": "subquery_budget_exhausted",
    "llm_call": "llm_budget_exhausted",
    "followup": "followup_budget_exhausted",
}


def deadline_passed(budget: AnalystBudget, usage: dict, now: float) -> bool:
    """True once `timeout_seconds` has elapsed since `usage["started_at"]`."""
    return (now - float(usage["started_at"])) >= budget.timeout_seconds


def can_afford(
    budget: AnalystBudget, usage: dict, now: float, kind: ChargeKind
) -> StopReason | None:
    """Returns the stop reason blocking one more `kind` action, or None.

    Checked before every charged action. The deadline is checked first for
    every kind, so no action at all starts after the deadline.
    """
    if deadline_passed(budget, usage, now):
        return "timeout"
    if usage[_COUNTER_FOR_KIND[kind]] >= _limit_for(budget, kind):
        return _EXHAUSTED_REASON[kind]
    return None


def spend(usage: dict, kind: ChargeKind) -> dict:
    """Returns a copy of `usage` with one `kind` action charged.

    Returns a new dict rather than mutating in place, so a node's returned
    state update is the only place a counter changes.
    """
    updated = dict(usage)
    counter = _COUNTER_FOR_KIND[kind]
    updated[counter] = int(updated[counter]) + 1
    return updated


def usage_summary(usage: dict, now: float) -> dict:
    """The public, UI-safe view of the counters: the raw counts plus elapsed
    seconds. Excludes the monotonic `started_at` clock value, which is
    meaningless outside this process."""
    return {
        "steps": int(usage["steps"]),
        "subqueries": int(usage["subqueries"]),
        "llm_calls": int(usage["llm_calls"]),
        "followups": int(usage["followups"]),
        "elapsed_seconds": round(now - float(usage["started_at"]), 3),
    }
