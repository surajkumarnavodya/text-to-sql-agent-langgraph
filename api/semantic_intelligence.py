"""REST surface for semantic intelligence (Prompt 35). Behind `ENABLE_SEMANTIC_INTELLIGENCE`.

Routes, all under `/semantic-intelligence`:
- POST   /run                              analyse one database's catalog, upsert findings
- GET    /findings                         the review queue, riskiest first
- POST   /findings/{id}/decision           accept or dismiss an open finding
- POST   /findings/{id}/rollback           restore an earlier finding version
- GET    /impact/{concept_key}             what depends on a concept, and what a table change would affect
- POST   /entries/{entry_id}/rollback      create a new draft from an older catalog version

Authorization reuses `semantic.catalog_policy.authorize_catalog_action`, the same
ABAC check every catalog route runs. Reading and deciding findings is
CATALOG_REVIEW (REVIEW). Rolling back is CATALOG_MANAGE (EDIT_DRAFT), since it
creates content. A resource in another tenant returns 404, not 403, so
existence is not disclosed.

Nothing here changes a published catalog entry. A finding decision is review
state only.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from identity.models import SemanticCatalogEntry, SemanticFinding, User
from identity.repositories.semantic_catalog import (
    entry_to_snapshot,
    get_entry_by_id,
    list_entries,
)
from identity.repositories.semantic_intelligence import (
    CatalogVersionNotFoundError,
    FindingVersionNotFoundError,
    InvalidFindingTransitionError,
    decide_finding,
    get_finding,
    list_findings,
    rollback_catalog_entry,
    rollback_finding,
    upsert_findings,
)
from identity.repositories.users import get_user_permissions
from semantic.catalog_policy import CatalogAction, authorize_catalog_action
from semantic.intelligence.engine import run_analysis
from semantic.intelligence.impact import impact_of_change
from sqlalchemy.orm import Session

from api.identity_authz import require_local_user
from api.semantic_intelligence_schemas import (
    DecisionRequest,
    EntryRollbackRequest,
    FindingOut,
    FindingRollbackRequest,
    ImpactOut,
    RunOut,
)
from config.settings import get_settings
from security.audit_log import log_security_event
from security.tenancy import resolve_actor_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/semantic-intelligence", tags=["semantic-intelligence"])

_TOP_FINDINGS_IN_RUN = 10


def _disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Semantic intelligence is not enabled."
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")


def _require_enabled() -> None:
    if not get_settings().enable_semantic_intelligence:
        raise _disabled()


def _authorize(
    user: User, session: Session, action: CatalogAction, resource_tenant_id: str | None
) -> None:
    """The same ABAC decision every catalog route makes. A cross-tenant resource
    is denied identically to a missing one."""
    decision = authorize_catalog_action(
        actor_permissions=get_user_permissions(session, user.id),
        actor_tenant_id=resolve_actor_tenant_id(user),
        entry_tenant_id=resource_tenant_id,
        action=action,
    )
    if not decision.allowed:
        if decision.reason == "cross_tenant":
            raise _not_found()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")


def _actor_tenant(user: User) -> str:
    """The caller's tenant, which every route below must have. A user with no
    tenant gets the same generic 403 a permission failure gets."""
    tenant_id = resolve_actor_tenant_id(user)
    if tenant_id is None:
        raise _forbidden()
    return tenant_id


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")


def _allowed_database(user: User, database_id: str) -> str:
    """The database must be one this tenant may use. Any other name is a 404,
    the same anti-enumeration answer a nonexistent database gets."""
    allowed = {d.name for d in get_settings().databases_for_tenant(_actor_tenant(user))}
    if database_id not in allowed:
        raise _not_found()
    return database_id


def _finding_out(row: SemanticFinding) -> FindingOut:
    content = row.content or {}
    return FindingOut(
        id=str(row.id),
        kind=row.kind,
        title=row.title,
        detail=content.get("detail", ""),
        status=row.status,
        risk_score=row.risk_score,
        risk_tier=row.risk_tier,
        confidence=row.confidence,
        truth_level=row.truth_level,
        version=row.version,
        reasons=list(content.get("reasons", [])),
        subjects=list(content.get("subjects", [])),
        evidence=list(content.get("evidence", [])),
        decision_note=row.decision_note,
    )


@router.post("/run", response_model=RunOut)
def run_analysis_route(
    database_id: str = Query(..., min_length=1, max_length=200),
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RunOut:
    """Analyses one database's catalog and upserts the findings. Read-only with
    respect to catalog content."""
    _require_enabled()
    user, session = user_and_session
    tenant_id = _actor_tenant(user)
    _authorize(user, session, CatalogAction.REVIEW, tenant_id)
    database_id = _allowed_database(user, database_id)

    settings = get_settings()
    entries = list_entries(session, tenant_id, database_id=database_id)
    result = run_analysis([entry_to_snapshot(e) for e in entries])
    # Findings are already riskiest-first, so the cap keeps the most important ones.
    kept = list(result.findings)[: settings.semantic_intelligence_max_findings]
    truncated = len(result.findings) - len(kept)
    summary = upsert_findings(
        session,
        tenant_id=tenant_id,
        database_id=database_id,
        findings=kept,
        now=datetime.now(UTC),
    )
    log_security_event(
        "semantic_intelligence_run",
        "info",
        "A semantic-intelligence analysis ran over a tenant's catalog.",
        database_id=database_id,
        created=summary.created,
        updated=summary.updated,
        actor_user_id=str(user.id),
    )
    queue = list_findings(session, tenant_id=tenant_id, database_id=database_id)
    return RunOut(
        database_id=database_id,
        entries_analysed=len(entries),
        findings_detected=len(result.findings),
        findings_truncated=truncated,
        created=summary.created,
        updated=summary.updated,
        unchanged=summary.unchanged,
        clusters=len(result.clusters),
        relationships=len(result.relationships),
        top_findings=[_finding_out(r) for r in queue[:_TOP_FINDINGS_IN_RUN]],
    )


@router.get("/findings", response_model=list[FindingOut])
def list_queue(
    database_id: str = Query(..., min_length=1, max_length=200),
    finding_status: str | None = Query(
        default=None, alias="status", pattern="^(open|accepted|dismissed)$"
    ),
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[FindingOut]:
    """The review queue for one database, riskiest first. Ordered by risk, not
    volume: a single high-risk conflict sits above any number of low-risk
    synonym candidates."""
    _require_enabled()
    user, session = user_and_session
    tenant_id = _actor_tenant(user)
    _authorize(user, session, CatalogAction.REVIEW, tenant_id)
    database_id = _allowed_database(user, database_id)
    rows = list_findings(
        session, tenant_id=tenant_id, database_id=database_id, status=finding_status
    )
    return [_finding_out(r) for r in rows]


@router.post("/findings/{finding_id}/decision", response_model=FindingOut)
def decide(
    finding_id: uuid.UUID,
    payload: DecisionRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> FindingOut:
    """Accepts or dismisses an open finding. Review state only: no catalog
    entry changes, and the finding stays AI_INFERENCE."""
    _require_enabled()
    user, session = user_and_session
    row = get_finding(session, tenant_id=_actor_tenant(user), finding_id=finding_id)
    if row is None:
        raise _not_found()
    _authorize(user, session, CatalogAction.REVIEW, row.tenant_id)
    try:
        decided = decide_finding(
            session,
            row,
            decision=payload.decision,
            user_id=user.id,
            note=payload.note,
            now=datetime.now(UTC),
        )
    except InvalidFindingTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _finding_out(decided)


@router.post("/findings/{finding_id}/rollback", response_model=FindingOut)
def rollback_finding_route(
    finding_id: uuid.UUID,
    payload: FindingRollbackRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> FindingOut:
    """Restores an earlier version of a finding as a new version, and reopens it."""
    _require_enabled()
    user, session = user_and_session
    row = get_finding(session, tenant_id=_actor_tenant(user), finding_id=finding_id)
    if row is None:
        raise _not_found()
    _authorize(user, session, CatalogAction.EDIT_DRAFT, row.tenant_id)
    try:
        restored = rollback_finding(
            session, row, to_version=payload.to_version, user_id=user.id, now=datetime.now(UTC)
        )
    except FindingVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _finding_out(restored)


@router.get("/impact/{concept_key}", response_model=ImpactOut)
def impact(
    concept_key: str,
    database_id: str = Query(..., min_length=1, max_length=200),
    proposed_source_tables: Annotated[list[str] | None, Query(max_length=50)] = None,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> ImpactOut:
    """What depends on `concept_key`, and what a source-table change would affect.
    A what-if list for review, not a prediction of what will break."""
    _require_enabled()
    user, session = user_and_session
    tenant_id = _actor_tenant(user)
    _authorize(user, session, CatalogAction.REVIEW, tenant_id)
    database_id = _allowed_database(user, database_id)
    entries = [
        entry_to_snapshot(e) for e in list_entries(session, tenant_id, database_id=database_id)
    ]
    try:
        report = impact_of_change(
            entries, concept_key, proposed_source_tables=proposed_source_tables
        )
    except KeyError as exc:
        raise _not_found() from exc
    return ImpactOut(
        concept_key=report.concept_key,
        risk_tier=report.risk_tier,
        dependents=list(report.dependents),
        removed_tables=list(report.removed_tables),
    )


@router.post("/entries/{entry_id}/rollback", response_model=dict)
def rollback_entry_route(
    entry_id: uuid.UUID,
    payload: EntryRollbackRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> dict:
    """Creates a new DRAFT from an older version's content. The published row is
    untouched, and the draft goes through the normal review and publish flow."""
    _require_enabled()
    user, session = user_and_session
    entry: SemanticCatalogEntry | None = get_entry_by_id(session, entry_id)
    if entry is None:
        raise _not_found()
    _authorize(user, session, CatalogAction.EDIT_DRAFT, entry.tenant_id)
    try:
        draft = rollback_catalog_entry(
            session, entry, target_version=payload.target_version, actor_user_id=user.id
        )
    except CatalogVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    log_security_event(
        "catalog_version_rolled_back",
        "info",
        "A catalog concept was rolled back to an earlier version as a new draft.",
        concept_key=entry.concept_key,
        target_version=payload.target_version,
        new_version=draft.version,
        actor_user_id=str(user.id),
    )
    return {"entry_id": str(draft.id), "version": draft.version, "status": draft.status}
