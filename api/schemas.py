"""Pydantic request/response models for `api/main.py`.

Deliberately thin: these mirror the subset of `agent.state.AgentState` the
Streamlit UI already renders (`ui/app.py`), not a new data model. Nothing
here re-decides what's safe to return -- the agent graph itself is what
already gates what ends up in `AgentState` (validated SQL only, row-capped
results, redacted errors).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.state import AgentStatus


class ConversationExchangeIn(BaseModel):
    """One prior turn, for follow-up reference resolution -- same shape as
    `agent.state.ConversationExchange`, accepted from an API caller instead
    of being built from `ui/session_history.py`'s server-side session
    state (the API has no server-side session of its own; the caller is
    responsible for resending recent turns each request)."""

    question: str
    sql: str | None = None
    tables: list[str] = Field(default_factory=list)
    status: str = "succeeded"


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Natural-language question.")
    conversation_history: list[ConversationExchangeIn] = Field(
        default_factory=list,
        description="Recent prior turns (oldest first), for follow-up resolution. Optional.",
    )
    enable_insight: bool = Field(
        default=True,
        description="Whether to attempt a plain-English insight sentence after execution.",
    )


class AttemptRecordOut(BaseModel):
    attempt: int
    sql: str | None = None
    outcome: str
    error: str | None = None
    will_retry: bool = False


class AskResponse(BaseModel):
    """Mirrors the fields of `agent.state.AgentState` that `ui/app.py`
    already surfaces to a human -- see that module for the reference
    rendering this response shape is kept consistent with."""

    status: AgentStatus
    sql: str | None = None
    result_columns: list[str] | None = None
    result_rows: list[list[Any]] | None = None
    row_count: int | None = None
    retry_count: int = 0
    attempt_history: list[AttemptRecordOut] = Field(default_factory=list)
    insight: str | None = None
    cost_notice: str | None = None
    rejection_reason: str | None = None
    rejection_message: str | None = None
    rate_limit_message: str | None = None
    clarification_message: str | None = None
    failure_explanation: str | None = None
    error_history: list[str] = Field(default_factory=list)


class ComponentHealth(BaseModel):
    ok: bool
    detail: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: ComponentHealth
    ollama: ComponentHealth
    schema_index: ComponentHealth


class ColumnOut(BaseModel):
    name: str
    type: str
    nullable: bool
    is_primary_key: bool


class TableOut(BaseModel):
    table_name: str
    columns: list[ColumnOut]


class TablesResponse(BaseModel):
    tables: list[TableOut]
