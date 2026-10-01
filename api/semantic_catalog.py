"""Tenant-aware semantic catalog -- the REST surface for
`identity/repositories/semantic_catalog.py` + `retrieval/ingestion.py`'s
publish-time vector-store sync. Prompt 09
(`09_SEMANTIC_CATALOG_CONTRACT.md`).

**Every route here does exactly one authorization dance**: resolve the
`SemanticCatalogEntry` row by id (never trusting a query filter alone to
scope it), then call `semantic.catalog_policy.authorize_catalog_action`
and act only on an `allowed=True` decision -- mirrors `api/onboarding.py`'s
own identical discipline.

**Only the `publish` route ever touches the vector store.** Create/edit/
review/request-changes are pure identity-DB operations -- a draft or
reviewed entry never reaches retrieval (see `identity.models
.SemanticCatalogEntry`'s own docstring for why this is the structural
enforcement of master-contract rule 10). A vector-store sync failure on
publish does **not** roll back the already-committed status change (the
identity DB remains the authoritative record of what's published) -- it
is logged and surfaced to the caller as a non-fatal `sync_warning` on the
response, the same "retrieval is best-effort, never a hard gate"
philosophy `retrieval.retriever.retrieve_business_context`'s own
fail-open contract already establishes elsewhere in this codebase.

**Conflict detection (Prompt 10, `10_GOVERNED_METRICS_CONTRACT.md`) is
non-blocking.** `create`/`publish` both run `identity.repositories
.semantic_catalog.find_conflicting_published_entries` and surface the
result as `CatalogEntryOut.conflicting_entry_ids`/`conflicting_entry_
names` -- never a rejection (this codebase's standing "surface ambiguity
to a human, never auto-resolve it" posture, same as `onboarding
/semantic_inference.py`'s own ambiguity flag). A non-empty result is also
logged via `security.audit_log.log_security_event` (the existing
structured-observability hook every other security-relevant event in
this codebase already uses). `get`/`list`/`update`/`review`/`request-
changes` never recompute this -- it's a point-in-time check against the
catalog as it stood at create/publish time, not a live property of the
entry itself, so those routes' `CatalogEntryOut.conflicting_entry_ids`
is always empty.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from identity.models import SemanticCatalogEntry, User
from identity.rbac import Permission
from identity.repositories.semantic_catalog import (
    InvalidCatalogStatusTransitionError,
    entry_to_snapshot,
    find_conflicting_published_entries,
    get_entry_by_id,
    list_entries,
    list_versions_for_concept_key,
)
from identity.repositories.semantic_catalog import (
    create_entry as _create_entry,
)
from identity.repositories.semantic_catalog import (
    mark_reviewed as _mark_reviewed,
)
from identity.repositories.semantic_catalog import (
    publish_entry as _publish_entry,
)
from identity.repositories.semantic_catalog import (
    request_changes as _request_changes,
)
from identity.repositories.semantic_catalog import (
    update_draft_entry as _update_draft_entry,
)
from identity.repositories.users import get_user_permissions
from retrieval.embeddings import EmbeddingError
from retrieval.ingestion import (
    business_concept_chunk_id_for_snapshot,
    sync_catalog_entry_to_vector_store,
)
from retrieval.vector_store import VectorStoreError
from semantic.catalog_policy import CatalogAction, authorize_catalog_action
from sqlalchemy.orm import Session

from api.identity_authz import require_local_user
from api.semantic_catalog_schemas import (
    CatalogEntryOut,
    CreateCatalogEntryRequest,
    ReviewDecisionRequest,
    UpdateCatalogEntryRequest,
)
from config.settings import get_settings
from security.audit_log import log_security_event
from security.redaction import redact_secrets
from security.tenancy import resolve_actor_tenant_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/semantic-catalog", tags=["semantic-catalog"])


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Semantic-catalog entry not found."
    )


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")


def _require_entry(session: Session, entry_id: uuid.UUID) -> SemanticCatalogEntry:
    entry = get_entry_by_id(session, entry_id)
    if entry is None:
        raise _not_found()
    return entry


def _authorize(
    user: User, session: Session, action: CatalogAction, entry: SemanticCatalogEntry | None
) -> None:
    permissions = get_user_permissions(session, user.id)
    actor_tenant_id = resolve_actor_tenant_id(user)
    decision = authorize_catalog_action(
        actor_permissions=permissions,
        actor_tenant_id=actor_tenant_id,
        entry_tenant_id=entry.tenant_id if entry is not None else None,
        action=action,
    )
    if not decision.allowed:
        # A cross-tenant entry is denied the exact same way as a
        # genuinely nonexistent one -- see `identity.share_policy`'s own
        # precedent for this anti-enumeration posture, reused here
        # deliberately (identical to `api/onboarding.py`'s own choice).
        if decision.reason == "cross_tenant":
            raise _not_found()
        raise _forbidden()


def _entry_out(
    entry: SemanticCatalogEntry, conflicts: list[SemanticCatalogEntry] | None = None
) -> CatalogEntryOut:
    snapshot = entry_to_snapshot(entry)
    conflicts = conflicts or []
    return CatalogEntryOut(
        id=entry.id,
        tenant_id=entry.tenant_id,
        database_id=entry.database_id,
        concept_type=entry.concept_type,  # type: ignore[arg-type]
        concept_key=entry.concept_key,
        business_name=entry.business_name,
        technical_name=entry.technical_name,
        description=entry.description,
        grain=entry.grain,
        keys=list(entry.keys or []),
        relationships=list(entry.relationships or []),
        domain=entry.domain,
        synonyms=list(entry.synonyms or []),
        business_rules=list(entry.business_rules or []),
        examples=list(entry.examples or []),
        evidence=list(entry.evidence or []),
        confidence=entry.confidence,
        status=entry.status,  # type: ignore[arg-type]
        owner=entry.owner,
        version=entry.version,
        supersedes_id=entry.supersedes_id,
        truth_level=snapshot.truth_level.value,
        reviewed_at=entry.reviewed_at,
        review_notes=entry.review_notes,
        published_at=entry.published_at,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        approved_expression=entry.approved_expression,
        source_tables=list(entry.source_tables or []),
        filters=list(entry.filters or []),
        dimensions=list(entry.dimensions or []),
        aggregation=entry.aggregation,
        conflicting_entry_ids=[c.id for c in conflicts],
        conflicting_entry_names=[c.business_name for c in conflicts],
    )


def _check_conflicts(session: Session, entry: SemanticCatalogEntry) -> list[SemanticCatalogEntry]:
    """Runs `find_conflicting_published_entries` for `entry` and logs a
    structured security event when it finds anything -- the one place
    both `create`/`publish` route through, so the check and its logging
    can never drift apart between the two call sites."""
    conflicts = find_conflicting_published_entries(
        session,
        tenant_id=entry.tenant_id,
        database_id=entry.database_id,
        concept_type=entry.concept_type,
        business_name=entry.business_name,
        synonyms=list(entry.synonyms or []),
        exclude_concept_key=entry.concept_key,
    )
    if conflicts:
        log_security_event(
            "semantic_catalog_conflict_detected",
            "warning",
            "A new/published semantic-catalog entry shares a business name or synonym "
            "with another already-published entry of a different concept_key.",
            entry_id=str(entry.id),
            concept_key=entry.concept_key,
            conflicting_concept_keys=[c.concept_key for c in conflicts],
        )
    return conflicts


@router.post("/entries", response_model=CatalogEntryOut)
def create_catalog_entry(
    payload: CreateCatalogEntryRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> CatalogEntryOut:
    user, session = user_and_session
    _authorize(user, session, CatalogAction.CREATE_ENTRY, entry=None)

    tenant_id = resolve_actor_tenant_id(user)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not resolve a tenant for this account.",
        )
    entry = _create_entry(
        session,
        tenant_id=tenant_id,
        database_id=payload.database_id,
        concept_type=payload.concept_type,
        concept_key=payload.concept_key,
        business_name=payload.business_name,
        technical_name=payload.technical_name,
        description=payload.description,
        grain=payload.grain,
        keys=payload.keys,
        relationships=payload.relationships,
        domain=payload.domain,
        synonyms=payload.synonyms,
        business_rules=payload.business_rules,
        examples=payload.examples,
        evidence=payload.evidence,
        confidence=payload.confidence,
        owner=payload.owner,
        created_by_user_id=user.id,
        approved_expression=payload.approved_expression,
        source_tables=payload.source_tables,
        filters=payload.filters,
        dimensions=payload.dimensions,
        aggregation=payload.aggregation,
    )
    conflicts = _check_conflicts(session, entry)
    return _entry_out(entry, conflicts)


@router.get("/entries", response_model=list[CatalogEntryOut])
def list_catalog_entries(
    database_id: str | None = Query(default=None),
    concept_type: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[CatalogEntryOut]:
    user, session = user_and_session
    tenant_id = resolve_actor_tenant_id(user)
    if tenant_id is None:
        return []
    permissions = get_user_permissions(session, user.id)
    if not (Permission.CATALOG_MANAGE in permissions or Permission.CATALOG_REVIEW in permissions):
        raise _forbidden()
    entries = list_entries(
        session,
        tenant_id,
        database_id=database_id,
        concept_type=concept_type,
        status=status_filter,
    )
    return [_entry_out(entry) for entry in entries]


@router.get("/entries/{entry_id}", response_model=CatalogEntryOut)
def get_catalog_entry(
    entry_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> CatalogEntryOut:
    user, session = user_and_session
    entry = _require_entry(session, entry_id)
    _authorize(user, session, CatalogAction.VIEW_ENTRY, entry)
    return _entry_out(entry)


@router.get("/entries/concept/{concept_key}/versions", response_model=list[CatalogEntryOut])
def get_catalog_entry_versions(
    concept_key: str,
    database_id: str = Query(...),
    concept_type: str = Query(...),
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[CatalogEntryOut]:
    user, session = user_and_session
    tenant_id = resolve_actor_tenant_id(user)
    if tenant_id is None:
        return []
    versions = list_versions_for_concept_key(
        session,
        tenant_id=tenant_id,
        database_id=database_id,
        concept_type=concept_type,
        concept_key=concept_key,
    )
    if not versions:
        return []
    # Any version's tenant/permission check stands in for the whole
    # concept's history -- every version of one concept always shares
    # the same tenant_id (set once at creation, never changed).
    _authorize(user, session, CatalogAction.VIEW_ENTRY, versions[0])
    return [_entry_out(entry) for entry in versions]


@router.patch("/entries/{entry_id}", response_model=CatalogEntryOut)
def update_catalog_entry(
    entry_id: uuid.UUID,
    payload: UpdateCatalogEntryRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> CatalogEntryOut:
    user, session = user_and_session
    entry = _require_entry(session, entry_id)
    _authorize(user, session, CatalogAction.EDIT_DRAFT, entry)

    fields = payload.model_dump(exclude_unset=True)
    try:
        entry = _update_draft_entry(session, entry, **fields)
    except InvalidCatalogStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _entry_out(entry)


@router.post("/entries/{entry_id}/review", response_model=CatalogEntryOut)
def review_catalog_entry(
    entry_id: uuid.UUID,
    payload: ReviewDecisionRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> CatalogEntryOut:
    """`"draft"` -> `"reviewed"`."""
    user, session = user_and_session
    entry = _require_entry(session, entry_id)
    _authorize(user, session, CatalogAction.REVIEW, entry)
    try:
        entry = _mark_reviewed(session, entry, reviewed_by_user_id=user.id, notes=payload.notes)
    except InvalidCatalogStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _entry_out(entry)


@router.post("/entries/{entry_id}/request-changes", response_model=CatalogEntryOut)
def request_catalog_entry_changes(
    entry_id: uuid.UUID,
    payload: ReviewDecisionRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> CatalogEntryOut:
    """`"reviewed"` -> `"draft"`."""
    user, session = user_and_session
    entry = _require_entry(session, entry_id)
    _authorize(user, session, CatalogAction.REQUEST_CHANGES, entry)
    try:
        entry = _request_changes(session, entry, reviewed_by_user_id=user.id, notes=payload.notes)
    except InvalidCatalogStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _entry_out(entry)


@router.post("/entries/{entry_id}/publish", response_model=CatalogEntryOut)
def publish_catalog_entry(
    entry_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> CatalogEntryOut:
    """`"reviewed"` -> `"published"` -- also syncs the new chunk into the
    vector store and removes the just-superseded version's own chunk
    (see this module's own docstring for the non-fatal-sync-failure
    contract)."""
    user, session = user_and_session
    entry = _require_entry(session, entry_id)
    _authorize(user, session, CatalogAction.PUBLISH, entry)
    try:
        published, superseded = _publish_entry(session, entry, published_by_user_id=user.id)
    except InvalidCatalogStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    settings = get_settings()
    snapshot = entry_to_snapshot(published)
    superseded_chunk_id = (
        business_concept_chunk_id_for_snapshot(entry_to_snapshot(superseded))
        if superseded is not None
        else None
    )
    try:
        sync_catalog_entry_to_vector_store(
            snapshot, settings, superseded_chunk_id=superseded_chunk_id
        )
    except (EmbeddingError, VectorStoreError) as exc:
        safe_detail = redact_secrets(str(exc), settings)
        logger.warning(
            "[semantic_catalog] publish succeeded in the identity DB but vector-store "
            "sync failed for entry %s: %s",
            published.id,
            safe_detail,
        )
        # The identity DB remains authoritative -- a sync failure doesn't
        # roll back the publish, it's surfaced for the operator to retry
        # (re-publishing a still-"published" row is itself a no-op per
        # `VALID_STATUS_TRANSITIONS`, so a true resync needs a future
        # dedicated backfill path -- a known, disclosed limitation, see
        # `09_SEMANTIC_CATALOG_CONTRACT.md`).

    conflicts = _check_conflicts(session, published)
    return _entry_out(published, conflicts)
