"""Repository layer for the tenant-aware semantic catalog --
`SemanticCatalogEntry`, Prompt 09 (`09_SEMANTIC_CATALOG_CONTRACT.md`).

**This module never makes an authorization decision itself** -- every
function here is a plain data operation; `semantic.catalog_policy
.authorize_catalog_action` is the only place a caller may or may not do
something is decided, mirroring `identity/repositories/onboarding.py`'s
own identical split.

**Status transitions are still defended here, not only at the API
layer** -- unlike `identity/repositories/onboarding.py` (which trusts its
caller's already-schema-validated `decision` literal), a wrong status
transition here would corrupt a concept's own version/supersession
history, not just one review-item decision, so `_assert_transition_valid`
raises `InvalidCatalogStatusTransitionError` against `semantic.catalog
.VALID_STATUS_TRANSITIONS` as a second, defense-in-depth check behind
`api/semantic_catalog.py`'s own pre-flight validation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from semantic.catalog import (
    VALID_STATUS_TRANSITIONS,
    CatalogConceptType,
    CatalogEntrySnapshot,
    CatalogStatus,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from identity.models import SemanticCatalogEntry


class InvalidCatalogStatusTransitionError(Exception):
    """Raised when a caller attempts a status transition
    `semantic.catalog.VALID_STATUS_TRANSITIONS` doesn't allow from the
    entry's current status (e.g. publishing a still-`draft` entry)."""


def _assert_transition_valid(entry: SemanticCatalogEntry, target: CatalogStatus) -> None:
    current = CatalogStatus(entry.status)
    if target not in VALID_STATUS_TRANSITIONS.get(current, frozenset()):
        raise InvalidCatalogStatusTransitionError(
            f"Cannot move a {current.value!r} entry to {target.value!r}."
        )


def next_version_for_concept_key(
    session: Session, *, tenant_id: str, database_id: str, concept_type: str, concept_key: str
) -> int:
    """The next version number for `concept_key` within this `(tenant_id,
    database_id, concept_type)` scope -- `1` for a brand-new concept."""
    current_max = session.scalar(
        select(SemanticCatalogEntry.version)
        .where(SemanticCatalogEntry.tenant_id == tenant_id)
        .where(SemanticCatalogEntry.database_id == database_id)
        .where(SemanticCatalogEntry.concept_type == concept_type)
        .where(SemanticCatalogEntry.concept_key == concept_key)
        .order_by(SemanticCatalogEntry.version.desc())
        .limit(1)
    )
    return (current_max or 0) + 1


def create_entry(
    session: Session,
    *,
    tenant_id: str,
    database_id: str,
    concept_type: str,
    concept_key: str,
    business_name: str,
    technical_name: str | None,
    description: str,
    grain: str | None,
    keys: list[str],
    relationships: list[dict[str, Any]],
    domain: str | None,
    synonyms: list[str],
    business_rules: list[str],
    examples: list[str],
    evidence: list[dict[str, Any]],
    confidence: float,
    owner: str | None,
    created_by_user_id: uuid.UUID | None,
) -> SemanticCatalogEntry:
    """Creates one new `"draft"` version of `concept_key` -- version `1`
    for a brand-new concept, or the next version if one already exists
    (e.g. editing a previously `"published"` entry always creates a new
    draft row rather than mutating that published row -- see
    `identity.models.SemanticCatalogEntry`'s own docstring)."""
    version = next_version_for_concept_key(
        session,
        tenant_id=tenant_id,
        database_id=database_id,
        concept_type=concept_type,
        concept_key=concept_key,
    )
    entry = SemanticCatalogEntry(
        tenant_id=tenant_id,
        database_id=database_id,
        concept_type=concept_type,
        concept_key=concept_key,
        business_name=business_name,
        technical_name=technical_name,
        description=description,
        grain=grain,
        keys=keys,
        relationships=relationships,
        domain=domain,
        synonyms=synonyms,
        business_rules=business_rules,
        examples=examples,
        evidence=evidence,
        confidence=confidence,
        status="draft",
        owner=owner,
        version=version,
        created_by_user_id=created_by_user_id,
    )
    session.add(entry)
    session.commit()
    return entry


def get_entry_by_id(session: Session, entry_id: uuid.UUID) -> SemanticCatalogEntry | None:
    return session.get(SemanticCatalogEntry, entry_id)


def list_entries(
    session: Session,
    tenant_id: str,
    *,
    database_id: str | None = None,
    concept_type: str | None = None,
    status: str | None = None,
) -> list[SemanticCatalogEntry]:
    stmt = select(SemanticCatalogEntry).where(SemanticCatalogEntry.tenant_id == tenant_id)
    if database_id is not None:
        stmt = stmt.where(SemanticCatalogEntry.database_id == database_id)
    if concept_type is not None:
        stmt = stmt.where(SemanticCatalogEntry.concept_type == concept_type)
    if status is not None:
        stmt = stmt.where(SemanticCatalogEntry.status == status)
    return list(session.scalars(stmt.order_by(SemanticCatalogEntry.created_at.desc())))


def list_versions_for_concept_key(
    session: Session,
    *,
    tenant_id: str,
    database_id: str,
    concept_type: str,
    concept_key: str,
) -> list[SemanticCatalogEntry]:
    """Every version of one concept, newest first -- the review-history
    view `GET /semantic-catalog/entries/concept/{concept_key}/versions`
    reports."""
    stmt = (
        select(SemanticCatalogEntry)
        .where(SemanticCatalogEntry.tenant_id == tenant_id)
        .where(SemanticCatalogEntry.database_id == database_id)
        .where(SemanticCatalogEntry.concept_type == concept_type)
        .where(SemanticCatalogEntry.concept_key == concept_key)
        .order_by(SemanticCatalogEntry.version.desc())
    )
    return list(session.scalars(stmt))


def update_draft_entry(
    session: Session, entry: SemanticCatalogEntry, **fields: Any
) -> SemanticCatalogEntry:
    """Mutates a `"draft"` entry in place -- the one row this table ever
    allows an in-place edit on (see `identity.models.SemanticCatalogEntry`'s
    own docstring). `fields` are plain column-name/value pairs; the caller
    (`api/semantic_catalog.py`) is responsible for only ever passing
    editable columns.

    Raises:
        InvalidCatalogStatusTransitionError: if `entry.status` isn't
            `"draft"` -- an edit is not itself a status transition, but
            this reuses the same guard for the one invariant that
            actually matters here ("a non-draft row is immutable").
    """
    if CatalogStatus(entry.status) != CatalogStatus.DRAFT:
        raise InvalidCatalogStatusTransitionError(
            f"Cannot edit a {entry.status!r} entry -- only a 'draft' entry is mutable."
        )
    for key, value in fields.items():
        setattr(entry, key, value)
    session.commit()
    return entry


def mark_reviewed(
    session: Session,
    entry: SemanticCatalogEntry,
    *,
    reviewed_by_user_id: uuid.UUID,
    notes: str | None = None,
) -> SemanticCatalogEntry:
    """`"draft"` -> `"reviewed"`: an SME has approved the content, but it
    is not yet live in retrieval (see `semantic.catalog.CatalogStatus
    .REVIEWED`'s own docstring for why this is a distinct state from
    `"published"`)."""
    _assert_transition_valid(entry, CatalogStatus.REVIEWED)
    entry.status = "reviewed"
    entry.reviewed_by_user_id = reviewed_by_user_id
    entry.reviewed_at = datetime.now(UTC)
    entry.review_notes = notes  # type: ignore[assignment]
    session.commit()
    return entry


def request_changes(
    session: Session,
    entry: SemanticCatalogEntry,
    *,
    reviewed_by_user_id: uuid.UUID,
    notes: str | None = None,
) -> SemanticCatalogEntry:
    """`"reviewed"` -> `"draft"`: sends an already-reviewed entry back for
    revision -- still a review decision (records the same reviewer/notes
    fields `mark_reviewed` does), just the opposite outcome."""
    _assert_transition_valid(entry, CatalogStatus.DRAFT)
    entry.status = "draft"
    entry.reviewed_by_user_id = reviewed_by_user_id
    entry.reviewed_at = datetime.now(UTC)
    entry.review_notes = notes  # type: ignore[assignment]
    session.commit()
    return entry


def publish_entry(
    session: Session,
    entry: SemanticCatalogEntry,
    *,
    published_by_user_id: uuid.UUID,
) -> tuple[SemanticCatalogEntry, SemanticCatalogEntry | None]:
    """`"reviewed"` -> `"published"`. If a prior `"published"` row already
    exists for the same `(tenant_id, database_id, concept_type,
    concept_key)`, it is flipped to `"superseded"` and linked via this
    entry's `supersedes_id` -- the concept's full version history stays
    inspectable, never overwritten (see `identity.models
    .SemanticCatalogEntry`'s own docstring).

    Returns:
        `(published_entry, superseded_entry_or_none)` -- the caller
        (`api/semantic_catalog.py`) uses the superseded entry's prior
        chunk id to remove it from the vector store (`retrieval.ingestion
        .sync_catalog_entry_to_vector_store`); `None` when this is the
        concept's first-ever published version.
    """
    _assert_transition_valid(entry, CatalogStatus.PUBLISHED)

    previous_published = session.scalar(
        select(SemanticCatalogEntry)
        .where(SemanticCatalogEntry.tenant_id == entry.tenant_id)
        .where(SemanticCatalogEntry.database_id == entry.database_id)
        .where(SemanticCatalogEntry.concept_type == entry.concept_type)
        .where(SemanticCatalogEntry.concept_key == entry.concept_key)
        .where(SemanticCatalogEntry.status == "published")
    )

    entry.status = "published"
    entry.published_by_user_id = published_by_user_id
    entry.published_at = datetime.now(UTC)
    if previous_published is not None:
        entry.supersedes_id = previous_published.id
        previous_published.status = "superseded"
    session.commit()
    return entry, previous_published


def entry_to_snapshot(entry: SemanticCatalogEntry) -> CatalogEntrySnapshot:
    """Converts a real ORM row into the ORM-independent `CatalogEntrySnapshot`
    shape `retrieval/`/`semantic/metrics.py` consume -- see
    `semantic.catalog`'s own module docstring for why neither of those
    modules ever imports `identity.models`/SQLAlchemy directly."""
    return CatalogEntrySnapshot(
        id=str(entry.id),
        tenant_id=entry.tenant_id,
        database_id=entry.database_id,
        concept_type=CatalogConceptType(entry.concept_type),
        concept_key=entry.concept_key,
        business_name=entry.business_name,
        technical_name=entry.technical_name,
        description=entry.description,
        grain=entry.grain,
        keys=tuple(entry.keys or ()),
        relationships=tuple(entry.relationships or ()),
        domain=entry.domain,
        synonyms=tuple(entry.synonyms or ()),
        business_rules=tuple(entry.business_rules or ()),
        examples=tuple(entry.examples or ()),
        evidence=tuple(entry.evidence or ()),
        confidence=entry.confidence,
        status=CatalogStatus(entry.status),
        owner=entry.owner,
        version=entry.version,
    )
