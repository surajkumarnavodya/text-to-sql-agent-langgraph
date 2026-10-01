"""Pydantic request/response models for `api/onboarding.py`. Mirrors
`identity/share_schemas.py`'s own conventions exactly: request models get
`str_strip_whitespace=True` + `extra="forbid"`, response models get
`frozen=True`.

**`db_password` never appears in any response model, ever.** It's a
request-only field on `CreateOnboardingJobRequest`/
`RunDiscoveryRequest`/`PublishJobRequest` -- used immediately to build a
throwaway connection, then discarded (see `identity.models.OnboardingJob`'s
own docstring for why it's never persisted). `OnboardingJobOut` reports
every *non-secret* connection field for display, and nothing else.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OnboardingJobStatus = Literal[
    "pending", "discovering", "awaiting_review", "publishing", "published", "failed", "cancelled"
]
ReviewItemType = Literal["pii_classification", "relationship", "semantic_label", "golden_question"]
ReviewDecision = Literal["pending", "confirmed", "rejected"]


class CreateOnboardingJobRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    database_label: str = Field(..., min_length=1, max_length=200)
    db_type: str = Field(..., min_length=1, max_length=32)
    db_host: str | None = None
    db_port: int | None = Field(default=None, gt=0, le=65535)
    db_name: str | None = None
    db_user: str | None = None
    db_password: str | None = None
    db_schema: str | None = None


class RunDiscoveryRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    db_password: str | None = None
    verify_relationships_with_data: bool = False
    verify_pii_with_data: bool = False


class PublishJobRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    db_password: str | None = None


class DecideReviewItemRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    decision: Literal["confirmed", "rejected"]
    notes: str | None = Field(default=None, max_length=2000)


class OnboardingJobOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    database_label: str
    db_type: str
    db_host: str | None
    db_port: int | None
    db_name: str | None
    db_user: str | None
    db_schema: str | None
    status: OnboardingJobStatus
    current_stage: str | None
    error_message: str | None
    retry_count: int
    discovery_summary: dict[str, Any] | None
    version: int
    created_at: datetime
    updated_at: datetime


class OnboardingReviewItemOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    item_type: ReviewItemType
    table_name: str | None
    column_name: str | None
    subject: str
    payload: dict[str, Any]
    confidence: float
    is_ambiguous: bool
    decision: ReviewDecision
    decided_at: datetime | None
    decision_notes: str | None
    created_at: datetime


class OnboardingArtifactOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    artifact_type: Literal["semantic_contract", "golden_questions", "evaluation_report"]
    content: dict[str, Any]
    version: int
    created_at: datetime
