"""Request and response models for `api/semantic_intelligence.py` (Prompt 35).

Requests forbid unknown fields, so a client cannot inject a tenant, owner or
truth level. Responses are frozen, the same convention as the other API
schema modules. Every finding is returned with `truth_level`, so a suggestion
is never presented as confirmed business truth.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DecisionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    decision: Literal["accept", "dismiss"]
    note: str | None = Field(default=None, max_length=2000)


class FindingRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_version: int = Field(..., ge=1)


class EntryRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_version: int = Field(..., ge=1)


class FindingOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    title: str
    detail: str
    status: str
    risk_score: int
    risk_tier: str
    confidence: float
    truth_level: str
    version: int
    reasons: list[str]
    subjects: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    decision_note: str | None


class RunOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    database_id: str
    entries_analysed: int
    findings_detected: int
    findings_truncated: int
    created: int
    updated: int
    unchanged: int
    clusters: int
    relationships: int
    top_findings: list[FindingOut]


class ImpactOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    concept_key: str
    risk_tier: str
    dependents: list[dict[str, Any]]
    removed_tables: list[str]
