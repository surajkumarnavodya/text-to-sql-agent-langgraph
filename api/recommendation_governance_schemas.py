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


class RecommendationFeedbackEventOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    recommendation_id: uuid.UUID
    from_status: RecommendationStatusLiteral | None
    to_status: RecommendationStatusLiteral
    actor_user_id: uuid.UUID | None
    actor_label: str | None
    reason: str | None
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
