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
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from identity.models import SemanticCatalogEntry


class InvalidCatalogStatusTransitionError(Exception):
    """Raised when a caller attempts a status transition
    `semantic.catalog.VALID_STATUS_TRANSITIONS` doesn't allow from the
    entry's current status (e.g. publishing a still-`draft` entry), *or*
    when `_apply_transition`'s own conditional `UPDATE` finds the row's
    real, committed status no longer matches what this caller's
    in-memory `entry` object expected -- see that function's own
    docstring for why both checks exist."""


def _assert_transition_valid(entry: SemanticCatalogEntry, target: CatalogStatus) -> None:
    """A fast, friendly first-pass check against the already-loaded
    in-memory `entry.status` -- correct for the overwhelmingly common
    non-concurrent case (e.g. rejecting an attempt to publish a
    still-draft entry outright), but **not** by itself a safe guard
    against a race between two concurrent callers (see
    `_apply_transition`, which is what actually prevents that)."""
    current = CatalogStatus(entry.status)
    if target not in VALID_STATUS_TRANSITIONS.get(current, frozenset()):
        raise InvalidCatalogStatusTransitionError(
            f"Cannot move a {current.value!r} entry to {target.value!r}."
        )


def _apply_transition(
    session: Session,
    entry: SemanticCatalogEntry,
    *,
    expected_current: CatalogStatus,
    values: dict[str, Any],
) -> None:
    """Atomically applies `values` (which may or may not include a
    `status` change) to `entry`'s row via one conditional `UPDATE ...
    WHERE id = entry.id AND status = expected_current`, rather than the
    "read in Python, mutate attributes, commit" pattern every caller used
    before this prompt.

    **Why this matters**: `_assert_transition_valid` (and
    `update_draft_entry`'s own "must still be draft" check) only ever
    inspect *this session's own, possibly stale, already-loaded*
    `entry.status` -- it says nothing about what another concurrent
    caller may have already committed. Two reviewers racing to publish
    the same reviewed entry (or one reviewer editing a draft at the exact
    moment another approves it to `"reviewed"`) could both pass that
    in-memory check and then both issue an unconditioned `UPDATE ... SET
    status = ... WHERE id = ...` that always "succeeds" regardless of the
    row's real current status -- silently losing the "only one caller's
    transition should ever win" guarantee the 409 response is supposed
    to provide (a real, found-and-fixed bug, not a hypothetical -- see
    `27_SME_SEMANTIC_REVIEW_DASHBOARD_CONTRACT.md`'s own concurrency
    test). The `WHERE status = expected_current` clause makes the
    database itself the single point of truth: at most one concurrent
    caller's `UPDATE` can ever match it, so at most one can ever report
    success for the same transition -- portable across every SQL dialect
    this app supports, no advisory lock or dialect-specific code needed.

    Raises:
        InvalidCatalogStatusTransitionError: if the row's real,
            currently-committed status no longer equals
            `expected_current` (rowcount 0) -- the race was caught.
    """
    if not values:
        # A no-op edit (e.g. `PATCH` with an empty body) -- nothing to
        # set, so there is no `UPDATE ... SET` to issue (an empty `SET`
        # clause is invalid SQL). Still re-reads the row fresh, the same
        # "reflect the real committed state" guarantee every other path
        # through this function provides.
        session.refresh(entry)
        return
    result = session.execute(
        sa_update(SemanticCatalogEntry)
        .where(SemanticCatalogEntry.id == entry.id)
        .where(SemanticCatalogEntry.status == expected_current.value)
        .values(**values)
    )
    if result.rowcount == 0:
        session.rollback()
        raise InvalidCatalogStatusTransitionError(
            f"Entry {entry.id} is no longer {expected_current.value!r} -- another "
            "reviewer's decision was already recorded."
        )
    session.commit()
    session.refresh(entry)


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
    approved_expression: str | None = None,
    source_tables: list[str] | None = None,
    filters: list[str] | None = None,
    dimensions: list[str] | None = None,
    aggregation: str | None = None,
) -> SemanticCatalogEntry:
    """Creates one new `"draft"` version of `concept_key` -- version `1`
    for a brand-new concept, or the next version if one already exists
    (e.g. editing a previously `"published"` entry always creates a new
    draft row rather than mutating that published row -- see
    `identity.models.SemanticCatalogEntry`'s own docstring).

    `approved_expression`/`source_tables`/`filters`/`dimensions`/
    `aggregation` (Prompt 10, `10_GOVERNED_METRICS_CONTRACT.md`) default
    to `None`/empty -- meaningful for a `METRIC`-type entry, left unset
    for every other `concept_type`.
    """
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
        approved_expression=approved_expression,
        source_tables=source_tables or [],
        filters=filters or [],
        dimensions=dimensions or [],
        aggregation=aggregation,
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
            actually matters here ("a non-draft row is immutable"). Also
            raised if the row's real, currently-committed status stopped
            being `"draft"` between this caller's read and this call
            (e.g. a reviewer approved it to `"reviewed"` in the same
            instant) -- see `_apply_transition`'s own docstring.
    """
    if CatalogStatus(entry.status) != CatalogStatus.DRAFT:
        raise InvalidCatalogStatusTransitionError(
            f"Cannot edit a {entry.status!r} entry -- only a 'draft' entry is mutable."
        )
    _apply_transition(session, entry, expected_current=CatalogStatus.DRAFT, values=dict(fields))
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
    _apply_transition(
        session,
        entry,
        expected_current=CatalogStatus.DRAFT,
        values={
            "status": CatalogStatus.REVIEWED.value,
            "reviewed_by_user_id": reviewed_by_user_id,
            "reviewed_at": datetime.now(UTC),
            "review_notes": notes,
        },
    )
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
    _apply_transition(
        session,
        entry,
        expected_current=CatalogStatus.REVIEWED,
        values={
            "status": CatalogStatus.DRAFT.value,
            "reviewed_by_user_id": reviewed_by_user_id,
            "reviewed_at": datetime.now(UTC),
            "review_notes": notes,
        },
    )
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

    values: dict[str, Any] = {
        "status": CatalogStatus.PUBLISHED.value,
        "published_by_user_id": published_by_user_id,
        "published_at": datetime.now(UTC),
    }
    if previous_published is not None:
        values["supersedes_id"] = previous_published.id
        # Flushed/committed atomically together with `entry`'s own
        # conditional UPDATE inside `_apply_transition` -- a lost race on
        # `entry` itself (rowcount 0) rolls this attribute change back
        # too, never leaving `previous_published` half-superseded.
        previous_published.status = "superseded"
    _apply_transition(session, entry, expected_current=CatalogStatus.REVIEWED, values=values)
    return entry, previous_published


def find_conflicting_published_entries(
    session: Session,
    *,
    tenant_id: str,
    database_id: str,
    concept_type: str,
    business_name: str,
    synonyms: list[str],
    exclude_concept_key: str,
) -> list[SemanticCatalogEntry]:
    """Finds every **other** `"published"` entry in this `(tenant_id,
    database_id, concept_type)` scope whose `business_name`/`synonyms`
    case-insensitively overlap with the given ones -- Prompt 10
    (`10_GOVERNED_METRICS_CONTRACT.md`)'s conflict-detection requirement:
    two different `concept_key`s both claiming the same business term
    (e.g. two "revenue" metrics with different formulas).

    Deliberately a plain Python-side set comparison over an already-
    scoped, already-small row set -- not a cross-dialect JSON-
    containment SQL query (SQLite/Postgres don't agree on one), and this
    scope is bounded by construction (one tenant's one database's one
    concept type's published entries, typically a handful).

    Never blocks anything -- the caller (`api/semantic_catalog.py`)
    surfaces the result for human review (this codebase's standing
    "surface ambiguity, never auto-resolve it" posture, same as
    `onboarding/semantic_inference.py`'s own ambiguity flag), it never
    becomes a rejection on its own.
    """
    candidate_terms = {term.strip().lower() for term in (business_name, *synonyms) if term.strip()}
    if not candidate_terms:
        return []

    published_entries = session.scalars(
        select(SemanticCatalogEntry)
        .where(SemanticCatalogEntry.tenant_id == tenant_id)
        .where(SemanticCatalogEntry.database_id == database_id)
        .where(SemanticCatalogEntry.concept_type == concept_type)
        .where(SemanticCatalogEntry.status == "published")
        .where(SemanticCatalogEntry.concept_key != exclude_concept_key)
    )

    conflicts: list[SemanticCatalogEntry] = []
    for candidate in published_entries:
        other_terms = {
            term.strip().lower()
            for term in (candidate.business_name, *(candidate.synonyms or ()))
            if term.strip()
        }
        if candidate_terms & other_terms:
            conflicts.append(candidate)
    return conflicts


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
        approved_expression=entry.approved_expression,
        source_tables=tuple(entry.source_tables or ()),
        filters=tuple(entry.filters or ()),
        dimensions=tuple(entry.dimensions or ()),
        aggregation=entry.aggregation,
    )
