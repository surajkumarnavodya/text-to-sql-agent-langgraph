"""Request and response models for POST /analyst/investigate (Prompt 33).

Response models are frozen, the same convention every other response in
`api/schemas.py` follows. The request forbids unknown fields, so a client
cannot slip in a tenant, role or budget override. Those values are server-side
only: roles and tenant come from the authenticated identity, and the budget
comes from `Settings`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from api.schemas import ConversationExchangeIn


class AnalystRequest(BaseModel):
    """One business question for the AI Data Analyst.

    The question's length cap is enforced downstream by
    `agent.input_guard.check_input` (config-driven), not duplicated here, for
    the same reason `AskRequest.question` leaves it out.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    question: str = Field(..., min_length=1)
    conversation_history: list[ConversationExchangeIn] = Field(default_factory=list)
    model: str | None = Field(
        default=None,
        description="An Ollama model from the configured allowlist. Omit for the default.",
    )


class AnalystSubquestionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    origin: str
    status: str
    evidence_ids: list[str]
    parent_evidence_id: str | None


class AnalystEvidenceOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    subquestion_id: str
    claim: str
    truth_level: str
    kind: str
    finding_kind: str | None
    period: str | None
    sql: str | None
    row_count: int | None


class AnalystRecommendationOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    subquestion_id: str
    claim: str
    truth_level: str
    category: str
    action: str | None
    rationale: str | None
    confidence: float | None
    evidence_ids: list[str]
    limitations: list[str]


class AnalystTraceOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    stage: str
    status: str
    detail: str


class AnalystUsageOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    steps: int
    subqueries: int
    llm_calls: int
    followups: int
    elapsed_seconds: float


class AnalystResponse(BaseModel):
    """The full, evidence-linked result of one analysis.

    `report_markdown` is built from fixed templates and typed values only.
    Every claim in it is labelled as either an observed database fact or an AI
    estimate, and cites the evidence ids it rests on.
    """

    model_config = ConfigDict(frozen=True)

    status: str
    stop_reason: str | None
    planning_mode: str | None
    subquestions: list[AnalystSubquestionOut]
    evidence: list[AnalystEvidenceOut]
    recommendations: list[AnalystRecommendationOut]
    open_items: list[str]
    trace: list[AnalystTraceOut]
    usage: AnalystUsageOut
    report_markdown: str


class SupervisedClaimOut(BaseModel):
    """One claim that survived validation and conflict resolution. Carries the
    truth level its agent is allowed to assert, so an estimate is never shown as
    an observed value."""

    model_config = ConfigDict(frozen=True)

    agent: str
    task_id: str
    text: str
    truth_level: str
    grounded_in: list[str]


class SupervisedConflictOut(BaseModel):
    """A disagreement the supervisor resolved. Lists the agents and the question,
    never the disagreeing values."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    key: str
    agents: list[str]
    outcome: str


class SupervisedSubquestionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    text: str


class SupervisedResponse(BaseModel):
    """The result of one supervised turn (Prompt 34). `violations` is a count
    only: the rejected content is never echoed back."""

    model_config = ConfigDict(frozen=True)

    status: str
    stop_reason: str | None
    subquestions: list[SupervisedSubquestionOut]
    claims: list[SupervisedClaimOut]
    conflicts: list[SupervisedConflictOut]
    violation_count: int
    open_items: list[str]
    trace: list[dict]
    usage: dict
    report_markdown: str
