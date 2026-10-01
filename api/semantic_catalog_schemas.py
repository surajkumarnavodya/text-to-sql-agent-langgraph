"""Pydantic request/response models for `api/semantic_catalog.py`.
Mirrors `api/onboarding_schemas.py`'s own conventions exactly: request
models get `str_strip_whitespace=True` + `extra="forbid"`, response
models get `frozen=True`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CatalogConceptTypeLiteral = Literal["entity", "metric", "dimension", "domain"]
CatalogStatusLiteral = Literal["draft", "reviewed", "published", "superseded"]


class CreateCatalogEntryRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    database_id: str = Field(..., min_length=1, max_length=200)
    concept_type: CatalogConceptTypeLiteral
    concept_key: str = Field(..., min_length=1, max_length=200)
    business_name: str = Field(..., min_length=1, max_length=200)
    technical_name: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=4000)
    grain: str | None = Field(default=None, max_length=500)
    keys: list[str] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    domain: str | None = Field(default=None, max_length=200)
    synonyms: list[str] = Field(default_factory=list)
    business_rules: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    owner: str | None = Field(default=None, max_length=200)


class UpdateCatalogEntryRequest(BaseModel):
    """Edits a `"draft"` entry in place -- every field optional, only the
    ones supplied are changed (see `api/semantic_catalog.py::update_entry`).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    business_name: str | None = Field(default=None, min_length=1, max_length=200)
    technical_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    grain: str | None = Field(default=None, max_length=500)
    keys: list[str] | None = None
    relationships: list[dict[str, Any]] | None = None
    domain: str | None = Field(default=None, max_length=200)
    synonyms: list[str] | None = None
    business_rules: list[str] | None = None
    examples: list[str] | None = None
    evidence: list[dict[str, Any]] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    owner: str | None = Field(default=None, max_length=200)


class ReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    notes: str | None = Field(default=None, max_length=2000)


class CatalogEntryOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    tenant_id: str
    database_id: str
    concept_type: CatalogConceptTypeLiteral
    concept_key: str
    business_name: str
    technical_name: str | None
    description: str
    grain: str | None
    keys: list[str]
    relationships: list[dict[str, Any]]
    domain: str | None
    synonyms: list[str]
    business_rules: list[str]
    examples: list[str]
    evidence: list[dict[str, Any]]
    confidence: float
    status: CatalogStatusLiteral
    owner: str | None
    version: int
    supersedes_id: uuid.UUID | None
    truth_level: str
    reviewed_at: datetime | None
    review_notes: str | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
