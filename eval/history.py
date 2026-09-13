"""Conversation-history bookkeeping for programmatically driving the agent.

Originally the same module the Streamlit UI used as its single source of
truth for "what was asked and what happened" (the UI has since been
removed in favor of the React dashboard, which keeps its own equivalent
client-side state -- see `frontend/src/lib/history.ts`). What survives here
is the part that isn't UI state at all: replaying a sequence of questions
as `run_agent()` calls and feeding prior successful turns back in as
follow-up context, exactly the way a live session would. The only
remaining callers are `eval/runner.py` and the deprecated
`scripts/run_eval.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from agent.state import AgentState, ConversationExchange

# How many of the most recent *successful* exchanges are handed to the agent
# as follow-up reference context (see agent/state.py's ConversationExchange).
MAX_FOLLOWUP_EXCHANGES = 3

AgentRunStatus = Literal["succeeded", "failed", "needs_clarification", "rejected", "rate_limited"]


@dataclass(frozen=True)
class QueryHistoryEntry:
    """One asked question and everything needed to replay it as follow-up context.

    Attributes:
        entry_id: Stable identifier for this entry.
        question: The exact natural-language text asked.
        sql: The agent's generated SQL (None if generation/classification
            never got that far, e.g. needs_clarification).
        agent_status: Raw `AgentState["status"]` at the end of the run.
        retry_count: Number of self-correction retries the agent used.
        row_count: Row count from the agent's own internal execution.
        tables: Table names the agent's SQL was generated against, used as
            the `tables` field of a `ConversationExchange` if this entry
            later becomes follow-up reference material.
        timestamp: When this question was asked.
        final_state: The full `AgentState` this run produced.
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


def build_conversation_history(
    history: list[QueryHistoryEntry], max_exchanges: int = MAX_FOLLOWUP_EXCHANGES
) -> list[ConversationExchange]:
    """Converts recent successful history into follow-up reference context.

    Only "succeeded" entries are eligible: a failed or needs_clarification
    entry has no reliable resolved SQL/tables to hand the model as reference
    material, and including one risks anchoring a follow-up to a query that
    never actually ran. Capped to the last `max_exchanges` *successful*
    entries (oldest first) so a long session's prompt size stays bounded --
    older exchanges drop off regardless of how many failed attempts sit
    between them and the cutoff.
    """
    successful = [e for e in history if e.agent_status == "succeeded"]
    capped = successful[-max_exchanges:] if max_exchanges > 0 else []
    return [
        ConversationExchange(question=e.question, sql=e.sql, tables=e.tables, status=e.agent_status)
        for e in capped
    ]
