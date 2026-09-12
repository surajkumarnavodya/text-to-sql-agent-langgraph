"""Pydantic request/response models for `api/main.py`.

Deliberately thin: these mirror the subset of `agent.state.AgentState` the
Streamlit UI already renders (`ui/app.py`), not a new data model. Nothing
here re-decides what's safe to return -- the agent graph itself is what
already gates what ends up in `AgentState` (validated SQL only, row-capped
results, redacted errors).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.state import AgentStatus


class ConversationExchangeIn(BaseModel):
    """One prior turn, for follow-up reference resolution -- same shape as
    `agent.state.ConversationExchange`, accepted from an API caller instead
    of being built from `ui/session_history.py`'s server-side session
    state (the API has no server-side session of its own; the caller is
    responsible for resending recent turns each request)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    question: str = Field(..., min_length=1)
    sql: str | None = None
    tables: list[str] = Field(default_factory=list)
    status: AgentStatus = "succeeded"


class AskRequest(BaseModel):
    """The upper bound on `question`'s length is deliberately NOT duplicated
    here as a `Field(max_length=...)` -- it's config-driven
    (`Settings.max_question_length`, default 500, overridable via `.env`)
    and already enforced downstream by `agent.input_guard.check_input`
    (the "too_long" `RejectionReason`), which is the single source of
    truth for it. A static schema-level cap would either hardcode a wrong
    number or drift from that setting the moment someone changes it."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    question: str = Field(..., min_length=1, description="Natural-language question.")
    conversation_history: list[ConversationExchangeIn] = Field(
        default_factory=list,
        description="Recent prior turns (oldest first), for follow-up resolution. Optional.",
    )
    enable_insight: bool = Field(
        default=True,
        description="Whether to attempt a plain-English insight sentence after execution.",
    )
    session_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Caller-supplied conversation identifier. Omit on the first turn of a "
            "conversation -- the API generates one and returns it in AskResponse. "
            "Pass the same value back on every subsequent turn of that conversation. "
            "Purely a correlation token today (the API is still stateless -- see "
            "ConversationExchangeIn's docstring); it does not yet gate or scope "
            "anything server-side, but is the identifier future server-side "
            "conversation state (e.g. the multi-source router) will key off of."
        ),
    )


class AttemptRecordOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt: int
    sql: str | None = None
    outcome: str
    error: str | None = None
    will_retry: bool = False


class AskResponse(BaseModel):
    """Mirrors the fields of `agent.state.AgentState` that `ui/app.py`
    already surfaces to a human -- see that module for the reference
    rendering this response shape is kept consistent with."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(
        description=(
            "Echoes AskRequest.session_id, or a freshly generated one if the "
            "caller didn't supply one -- pass this back on the next request "
            "in the same conversation."
        )
    )
    status: AgentStatus
    database: str | None = Field(
        default=None,
        description=(
            "Which configured database (Settings.databases[i].name) this question "
            "was auto-routed to. 'default' for a plain single-database setup."
        ),
    )
    sql: str | None = None
    result_columns: list[str] | None = None
    result_rows: list[list[Any]] | None = None
    row_count: int | None = None
    retry_count: int = 0
    attempt_history: list[AttemptRecordOut] = Field(default_factory=list)
    insight: str | None = None
    cost_notice: str | None = None
    low_confidence_notice: str | None = Field(
        default=None,
        description=(
            "Set when the query joined multiple tables and returned 0 rows -- a "
            "detection-only signal (see agent.state.AgentState.low_confidence_notice), "
            "not necessarily an error."
        ),
    )
    rejection_reason: str | None = None
    rejection_message: str | None = None
    rate_limit_message: str | None = None
    clarification_message: str | None = None
    failure_explanation: str | None = None
    error_history: list[str] = Field(default_factory=list)


class ComponentHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    detail: str


class DatabaseHealth(BaseModel):
    """Per-configured-database reachability + schema-index status -- one
    entry per `Settings.databases[i]` ('default' for a plain
    single-database setup)."""

    model_config = ConfigDict(frozen=True)

    name: str
    connection: ComponentHealth
    schema_index: ComponentHealth


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    databases: list[DatabaseHealth]
    ollama: ComponentHealth


class ColumnOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    nullable: bool
    is_primary_key: bool


class TableOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    database: str
    table_name: str
    columns: list[ColumnOut]


class TablesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    tables: list[TableOut]
