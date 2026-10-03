"""Pydantic request/response models for `api/recommendation_governance.py`.
Mirrors `api/semantic_catalog_schemas.py`'s own conventions exactly:
request models get `str_strip_whitespace=True` + `extra="forbid"`,
response models get `frozen=True`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RecommendationStatusLiteral = Literal[
    "generated",
    "reviewed",
    "accepted",
    "rejected",
    "partially_useful",
    "incorrect",
    "resolved",
    "expired",
]

#: The subset `POST /recommendations/{id}/feedback` accepts -- see
#: `recommendation.governance.FEEDBACK_VERDICT_STATUSES`'s own docstring
#: for why `resolved`/`expired` each get their own dedicated route
#: instead.
FeedbackVerdictLiteral = Literal[
    "reviewed", "accepted", "rejected", "partially_useful", "incorrect"
]


#: Prompt 31 -- what a feedback event records. `status_change` is every
#: event that existed before Prompt 31 (the migration's server default).
FeedbackEventTypeLiteral = Literal["status_change", "note", "owner_assigned"]


class RecommendationFeedbackEventOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    recommendation_id: uuid.UUID
    from_status: RecommendationStatusLiteral | None
    to_status: RecommendationStatusLiteral
    #: Prompt 31 -- additive, defaulted, so existing clients are unaffected.
    event_type: FeedbackEventTypeLiteral = "status_change"
    actor_user_id: uuid.UUID | None
    actor_label: str | None
    #: For `status_change` this is the verdict's reason; for `note` it is the
    #: note text itself.
    reason: str | None
    #: Typed payload for non-status events (`owner_assigned` carries the
    #: new and previous owner ids). `None` for status changes.
    detail: dict[str, Any] | None = None
    recommendation_version: str
    evidence_version: str
    created_at: datetime


class RecommendationRecordOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: str
    database_id: str
    category: str | None
    kind: str
    rule_or_model: str | None
    claim_text: str
    rationale: str | None
    affected_entity: str | None
    action: str | None
    measurable_impact: str | None
    confidence: float | None
    evidence: list[dict[str, Any]]
    limitations: list[str]
    engine_version: str
    evidence_version: str
    status: RecommendationStatusLiteral
    generated_at: datetime
    source_question: str | None
    source_sql: str | None
    created_at: datetime
    updated_at: datetime
    #: Prompt 31 -- additive. `owner_display_name` is `None` when the owner
    #: has no display name set, never an email address.
    owner_user_id: uuid.UUID | None = None
    owner_display_name: str | None = None


class AddNoteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note: str = Field(min_length=1, max_length=4000)


class AssignOwnerRequest(BaseModel):
    """`owner_user_id: null` clears the owner. The named user must be an
    active account in the same tenant -- anything else is the same 404 a
    nonexistent user gets (see `api/recommendation_governance.py`)."""

    model_config = ConfigDict(extra="forbid")

    owner_user_id: uuid.UUID | None = None


class SubmitFeedbackRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    status: FeedbackVerdictLiteral
    reason: str | None = Field(default=None, max_length=4000)


class ResolveRecommendationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str | None = Field(default=None, max_length=4000)


class ExpireRecommendationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str | None = Field(default=None, max_length=4000)


class RecommendationQualityMetricsOut(BaseModel):
    """`identity.repositories.recommendation_governance
    .quality_metrics_for_tenant`'s output, typed for the API surface --
    the literal "expose APIs for later dashboards" requirement."""

    model_config = ConfigDict(frozen=True)

    total: int
    by_status: dict[str, int]
    by_category: dict[str, dict[str, int]]
    judged_total: int
    acceptance_rate: float | None
