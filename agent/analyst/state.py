"""Typed state for the AI Data Analyst agent (Prompt 33).

Same conventions as `agent.state.AgentState`: `typing_extensions.TypedDict`
(required for LangGraph's pydantic schema generation on Python < 3.12),
plain dicts rather than model instances, and `operator.add` reducers on the
fields that accumulate across nodes (trace, evidence, open items,
recommendations). Every field a node does not own is read, never written.

The analyst never stores raw result rows here. Evidence carries a claim, its
truth level, the SQL that produced it and a row count, so the state stays
bounded no matter how large the underlying result set was.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal

from typing_extensions import TypedDict

from agent.state import ConversationExchange

# How the analysis as a whole ended. Set once, by the explain node.
AnalystStatus = Literal[
    "succeeded",
    "partial",
    "insufficient_data",
    "needs_clarification",
    "rejected",
    "failed",
]

# Where one sub-question's run landed.
SubquestionStatus = Literal[
    "pending",
    "succeeded",
    "empty",
    "needs_clarification",
    "blocked",
    "rejected",
    "failed",
    "rate_limited",
]

SubquestionOrigin = Literal["user", "planner", "investigation"]


class Subquestion(TypedDict):
    """One unit of work: a natural-language question sent to the governed
    Text-to-SQL pipeline (`agent.graph.run_agent`).

    `origin` says where the text came from. Planner and investigation text is
    model output, so it is always labelled as an AI decomposition, never as
    the user's own words. `parent_evidence_id` is set only for investigation
    follow-ups and links the follow-up back to the evidence that triggered it.
    """

    id: str
    text: str
    origin: SubquestionOrigin
    status: SubquestionStatus
    evidence_ids: list[str]
    parent_evidence_id: str | None


class EvidenceItem(TypedDict):
    """One claim the final answer may cite, with its provenance.

    `truth_level` is always copied from the source claim (a
    `DataTruthLevel` value string): `DATABASE_FACT` for a value the database
    returned or a deterministic statistic over it, `AI_INFERENCE` for a
    suggestion. This module never upgrades a level.
    """

    id: str
    subquestion_id: str
    claim: str
    truth_level: str
    kind: str  # "row_count" | "analytics_finding" | "recommendation_basis"
    finding_kind: str | None
    period: str | None
    sql: str | None
    row_count: int | None


class TraceEntry(TypedDict):
    """One step in the analysis timeline. Trace entries are the audit surface
    for the whole run: every intermediate artifact is traceable to one of
    these, in order."""

    seq: int
    stage: str  # "understand" | "execute" | "analyze" | "recommend" | "explain"
    status: str
    detail: str


class RecommendationCandidate(TypedDict):
    """A recommendation as the analysis received it, tagged with the
    sub-question that produced it. Deduplicated and linked to evidence by the
    recommend node."""

    subquestion_id: str
    recommendation: dict


class AnalystState(TypedDict, total=False):
    """Full state for one analysis run.

    Input fields are set once by the caller (`api/analyst.py`) and never
    mutated. Everything else is written by exactly one node, as documented on
    each field.
    """

    # --- Input, set once by the caller ---
    question: str
    caller_roles: tuple[str, ...]
    caller_subject: str | None
    tenant_id: str | None
    model: str | None
    conversation_history: list[ConversationExchange]
    budget: dict  # AnalystBudget.to_dict()

    # --- Owned by the graph ---
    usage: dict  # agent.analyst.budget counters + started_at
    subquestions: list[Subquestion]
    evidence: Annotated[list[EvidenceItem], operator.add]
    recommendation_candidates: Annotated[list[RecommendationCandidate], operator.add]
    open_items: Annotated[list[str], operator.add]
    trace: Annotated[list[TraceEntry], operator.add]
    stop_reason: str | None
    status: AnalystStatus | None
    report_markdown: str | None
    recommendations: list[dict]
    # The planner's own outcome, for the trace and for the UI's
    # "planned by" note: "planner", or "fallback" when the planner failed
    # open to the plain single-question path.
    planning_mode: str | None
