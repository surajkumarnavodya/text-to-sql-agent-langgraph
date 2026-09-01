"""Session-only query history: state shape + pure helper functions.

Deliberately separate from `ui/app.py` (which owns *when* these are called
and how they're rendered) so the state-management logic itself -- add,
clear, cap-and-convert-for-follow-up-resolution -- is plain, dependency-free
Python that can be unit tested without a running Streamlit app (see
`tests/test_session_history.py`).

This is also Part 1's single source of truth for follow-up context (see
CLAUDE.md's Part 3 integration note): `build_conversation_history` is what
turns this same history into the `ConversationExchange` list passed to
`agent.graph.run_agent`, so there is exactly one place "what was asked and
what happened" lives -- not a separate parallel state for the agent versus
the UI.

Everything here is in-memory / `st.session_state`-scoped only, per project
scope: no persistence to disk, no survival across a browser refresh or app
restart (see the "Clear history" caption in `ui/app.py`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from agent.state import AgentState, ConversationExchange

# How many of the most recent *successful* exchanges are handed to the agent
# as follow-up reference context (see agent/state.py's ConversationExchange
# and CLAUDE.md's "cap how far back context reaches" requirement). This is
# independent of how many entries the History panel itself displays -- the
# panel shows the full session; only follow-up resolution is capped.
MAX_FOLLOWUP_EXCHANGES = 3

AgentRunStatus = Literal["succeeded", "failed", "needs_clarification", "rejected", "rate_limited"]


@dataclass(frozen=True)
class QueryHistoryEntry:
    """One asked question and everything needed to either re-view or re-run it.

    Attributes:
        entry_id: Stable identifier for Streamlit widget keys (View/Re-run
            buttons need a key that survives reruns without colliding).
        question: The exact natural-language text asked.
        sql: The agent's generated SQL (None if generation/classification
            never got that far, e.g. needs_clarification).
        agent_status: Raw `AgentState["status"]` at the end of the run --
            "succeeded" | "failed" | "needs_clarification" | "rejected". Kept distinct
            from `retry_count` (below) so a display-only "retried" badge
            doesn't get conflated with the actual outcome.
        retry_count: Number of self-correction retries the agent used.
        row_count: Row count from the agent's own internal execution (not
            necessarily what's currently displayed -- see confirmed_rows).
        tables: Table names the agent's SQL was generated against, used both
            for display and as the `tables` field of a `ConversationExchange`
            if this entry later becomes follow-up reference material.
        timestamp: When this question was asked.
        final_state: The full `AgentState`, kept so "View" can restore the
            retry timeline / schema context panels without re-running.
        confirmed_columns / confirmed_rows / confirmed_error: What the user
            actually saw after clicking "Confirm and Run" for this turn (None
            until they do). Distinct from the agent's internal execution --
            see ui/app.py's module docstring on why nothing is shown until
            confirmed. "View" restores exactly this, never the agent's
            internal (unconfirmed) execution result.
    """

    entry_id: str
    question: str
    sql: str | None
    agent_status: AgentRunStatus
    retry_count: int
    row_count: int | None
    tables: list[str]
    timestamp: datetime
    final_state: AgentState
    confirmed_columns: list[str] | None = None
    confirmed_rows: list[tuple] | None = None
    confirmed_error: str | None = None


def new_history_entry(question: str, final_state: AgentState) -> QueryHistoryEntry:
    """Builds a `QueryHistoryEntry` from a completed `run_agent()` call."""
    tables = [t["table_name"] for t in final_state.get("schema_tables") or []]
    return QueryHistoryEntry(
        entry_id=uuid.uuid4().hex,
        question=question,
        sql=final_state.get("sql"),
        agent_status=final_state.get("status", "failed"),  # type: ignore[arg-type]
        retry_count=final_state.get("retry_count", 0),
        row_count=final_state.get("row_count"),
        tables=tables,
        timestamp=datetime.now(),
        final_state=final_state,
    )


def append_entry(
    history: list[QueryHistoryEntry], entry: QueryHistoryEntry
) -> list[QueryHistoryEntry]:
    """Returns a new list with `entry` appended (oldest first)."""
    return [*history, entry]


def clear_history() -> list[QueryHistoryEntry]:
    """Returns an empty history -- the "Clear history" action's whole implementation."""
    return []


def with_confirmed_result(
    entry: QueryHistoryEntry, columns: list[str], rows: list[tuple]
) -> QueryHistoryEntry:
    """Returns a copy of `entry` recording what "Confirm and Run" actually displayed."""
    return replace(entry, confirmed_columns=columns, confirmed_rows=rows, confirmed_error=None)


def with_confirmed_error(entry: QueryHistoryEntry, error: str) -> QueryHistoryEntry:
    """Returns a copy of `entry` recording a "Confirm and Run" execution failure."""
    return replace(entry, confirmed_columns=None, confirmed_rows=None, confirmed_error=error)


def replace_entry(
    history: list[QueryHistoryEntry], entry_id: str, updated: QueryHistoryEntry
) -> list[QueryHistoryEntry]:
    """Returns a new list with the entry matching `entry_id` swapped for `updated`."""
    return [updated if e.entry_id == entry_id else e for e in history]


def build_conversation_history(
    history: list[QueryHistoryEntry], max_exchanges: int = MAX_FOLLOWUP_EXCHANGES
) -> list[ConversationExchange]:
    """Converts recent successful history into follow-up reference context.

    Only "succeeded" entries are eligible: a failed or needs_clarification
    entry has no reliable resolved SQL/tables to hand the model as reference
    material, and including one risks anchoring a follow-up to a query that
    never actually ran. Capped to the last `max_exchanges` *successful*
    entries (oldest first) so a long session's prompt size stays bounded
    (CLAUDE.md's "cap how far back context reaches" requirement) -- older
    exchanges drop off regardless of how many failed attempts sit between
    them and the cutoff.
    """
    successful = [e for e in history if e.agent_status == "succeeded"]
    capped = successful[-max_exchanges:] if max_exchanges > 0 else []
    return [
        ConversationExchange(question=e.question, sql=e.sql, tables=e.tables, status=e.agent_status)
        for e in capped
    ]


_STATUS_DISPLAY: dict[AgentRunStatus, tuple[str, str]] = {
    "succeeded": ("✅", "succeeded"),
    "failed": ("❌", "failed"),
    "needs_clarification": ("❓", "needs clarification"),
    "rejected": ("\U0001f6ab", "rejected"),
    "rate_limited": ("\U0001f40c", "rate limited"),
}


def status_label(entry: QueryHistoryEntry) -> tuple[str, str]:
    """Returns (icon, label) for the History panel's compact status indicator.

    "succeeded" with `retry_count > 0` is relabeled "retried" -- the
    self-correction loop needed at least one more attempt, worth surfacing
    at a glance even though the final outcome was still success.
    """
    icon, label = _STATUS_DISPLAY.get(entry.agent_status, ("❓", entry.agent_status))
    if entry.agent_status == "succeeded" and entry.retry_count > 0:
        return "\U0001f501", "retried"
    return icon, label
