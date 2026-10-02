"""The governed recommendation lifecycle's REST surface -- the API for
`identity/repositories/recommendation_governance.py`. Prompt 18
(`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`).

**Every route here does exactly one authorization dance**: resolve the
`RecommendationRecord` row by id (never trusting a query filter alone to
scope it), then call `recommendation.governance_policy
.authorize_recommendation_action` and act only on an `allowed=True`
decision -- mirrors `api/semantic_catalog.py`'s own identical discipline.

**There is no create/upload route here.** A `RecommendationRecord` is
only ever created by the live `/ask` pipeline's own already-authorized
call (`api/recommendation_persistence.py`, itself gated by
`agent.authz.Permission.ASK` well before this module is ever reached) --
this surface is read + feedback + lifecycle only, never ingestion.

**No route here ever changes `recommendation.engine`'s own rules or
thresholds.** Master-contract requirement ("feedback must not
automatically alter production rules from a single event") holds
structurally: every write this module performs lands only in
`recommendation_records`/`recommendation_feedback_events`, which nothing
in `recommendation/engine.py` or `config/settings.py` ever reads.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from identity.models import RecommendationFeedbackEvent, RecommendationRecord, User
from identity.rbac import Permission
from identity.repositories.recommendation_governance import (
    InvalidRecommendationStatusTransitionError,
    expire_record,
    get_record_by_id,
    list_feedback_events,
    list_records,
    mark_resolved,
    quality_metrics_for_tenant,
)
from identity.repositories.recommendation_governance import (
    submit_feedback as _submit_feedback,
)
from identity.repositories.users import get_user_permissions
from recommendation.governance import RecommendationStatus
from recommendation.governance_policy import RecommendationAction, authorize_recommendation_action
from sqlalchemy.orm import Session

from api.identity_authz import require_local_user
from api.recommendation_governance_schemas import (
    ExpireRecommendationRequest,
    RecommendationFeedbackEventOut,
    RecommendationQualityMetricsOut,
    RecommendationRecordOut,
    ResolveRecommendationRequest,
    SubmitFeedbackRequest,
)
from security.tenancy import resolve_actor_tenant_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/recommendations", tags=["recommendation-governance"])


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recommendation not found.")


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")


def _require_record(session: Session, record_id: uuid.UUID) -> RecommendationRecord:
    record = get_record_by_id(session, record_id)
    if record is None:
        raise _not_found()
    return record


def _authorize(
    user: User, session: Session, action: RecommendationAction, record: RecommendationRecord
) -> None:
    permissions = get_user_permissions(session, user.id)
    actor_tenant_id = resolve_actor_tenant_id(user)
    decision = authorize_recommendation_action(
        actor_permissions=permissions,
        actor_tenant_id=actor_tenant_id,
        record_tenant_id=record.tenant_id,
        action=action,
    )
    if not decision.allowed:
        # A cross-tenant record is denied the exact same way as a
        # genuinely nonexistent one -- see `identity.share_policy`'s own
        # anti-enumeration precedent, reused here deliberately (identical
        # to `api/semantic_catalog.py`'s own choice).
        if decision.reason == "cross_tenant":
            raise _not_found()
        raise _forbidden()


def _record_out(record: RecommendationRecord) -> RecommendationRecordOut:
    return RecommendationRecordOut(
        id=record.id,
        tenant_id=record.tenant_id,
        database_id=record.database_id,
        category=record.category,
        kind=record.kind,
        rule_or_model=record.rule_or_model,
        claim_text=record.claim_text,
        rationale=record.rationale,
        affected_entity=record.affected_entity,
        action=record.action,
        measurable_impact=record.measurable_impact,
        confidence=record.confidence,
        evidence=list(record.evidence or []),
        limitations=list(record.limitations or []),
        engine_version=record.engine_version,
        evidence_version=record.evidence_version,
        status=record.status,  # type: ignore[arg-type]
        generated_at=record.generated_at,
        source_question=record.source_question,
        source_sql=record.source_sql,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _event_out(event: RecommendationFeedbackEvent) -> RecommendationFeedbackEventOut:
    return RecommendationFeedbackEventOut(
        id=event.id,
        recommendation_id=event.recommendation_id,
        from_status=event.from_status,  # type: ignore[arg-type]
        to_status=event.to_status,  # type: ignore[arg-type]
        actor_user_id=event.actor_user_id,
        actor_label=event.actor_label,
        reason=event.reason,
        recommendation_version=event.recommendation_version,
        evidence_version=event.evidence_version,
        created_at=event.created_at,
    )


@router.get("/metrics", response_model=RecommendationQualityMetricsOut)
def get_recommendation_quality_metrics(
    database_id: str | None = Query(default=None),
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationQualityMetricsOut:
    """The literal "recommendation quality can be measured" acceptance
    criterion -- a plain, read-only aggregate rollup for a future
    dashboard. Registered before `/{record_id}` so a literal path
    segment "metrics" is never swallowed as a (malformed) UUID path
    parameter."""
    user, session = user_and_session
    tenant_id = resolve_actor_tenant_id(user)
    if tenant_id is None:
        return RecommendationQualityMetricsOut(
            total=0, by_status={}, by_category={}, judged_total=0, acceptance_rate=None
        )
    permissions = get_user_permissions(session, user.id)
    if not (
        Permission.RECOMMENDATION_REVIEW in permissions
        or Permission.RECOMMENDATION_MANAGE in permissions
    ):
        raise _forbidden()
    metrics = quality_metrics_for_tenant(session, tenant_id, database_id=database_id)
    return RecommendationQualityMetricsOut(**metrics)


@router.get("", response_model=list[RecommendationRecordOut])
def list_recommendations(
    database_id: str | None = Query(default=None),
    category: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[RecommendationRecordOut]:
    user, session = user_and_session
    tenant_id = resolve_actor_tenant_id(user)
    if tenant_id is None:
        return []
    permissions = get_user_permissions(session, user.id)
    if not (
        Permission.RECOMMENDATION_REVIEW in permissions
        or Permission.RECOMMENDATION_MANAGE in permissions
    ):
        raise _forbidden()
    records = list_records(
        session, tenant_id, database_id=database_id, category=category, status=status_filter
    )
    return [_record_out(record) for record in records]


@router.get("/{record_id}", response_model=RecommendationRecordOut)
def get_recommendation(
    record_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationRecordOut:
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.VIEW, record)
    return _record_out(record)


@router.get("/{record_id}/events", response_model=list[RecommendationFeedbackEventOut])
def get_recommendation_events(
    record_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[RecommendationFeedbackEventOut]:
    """The full, append-only audit trail for one recommendation -- "capture
    actor, reason, timestamp, recommendation/evidence versions," exposed
    for a future dashboard."""
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.VIEW, record)
    events = list_feedback_events(session, record.id)
    return [_event_out(event) for event in events]


@router.post("/{record_id}/feedback", response_model=RecommendationRecordOut)
def submit_recommendation_feedback(
    record_id: uuid.UUID,
    payload: SubmitFeedbackRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationRecordOut:
    """Records one reviewed/accepted/rejected/partially_useful/incorrect
    verdict -- `resolved`/`expired` each have their own dedicated route
    below (never reachable through this one, see
    `recommendation.governance.FEEDBACK_VERDICT_STATUSES`'s own
    docstring)."""
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.SUBMIT_FEEDBACK, record)
    try:
        record = _submit_feedback(
            session,
            record,
            target_status=RecommendationStatus(payload.status),
            actor_user_id=user.id,
            reason=payload.reason,
        )
    except InvalidRecommendationStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _record_out(record)


@router.post("/{record_id}/resolve", response_model=RecommendationRecordOut)
def resolve_recommendation(
    record_id: uuid.UUID,
    payload: ResolveRecommendationRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationRecordOut:
    """`accepted`/`partially_useful` -> `resolved`."""
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.MARK_RESOLVED, record)
    try:
        record = mark_resolved(session, record, actor_user_id=user.id, reason=payload.reason)
    except InvalidRecommendationStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _record_out(record)


@router.post("/{record_id}/expire", response_model=RecommendationRecordOut)
def expire_recommendation(
    record_id: uuid.UUID,
    payload: ExpireRecommendationRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationRecordOut:
    """Any non-terminal status -> `expired` -- administrative override,
    `Permission.RECOMMENDATION_MANAGE` only (see
    `recommendation.governance_policy`'s own docstring)."""
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.EXPIRE, record)
    try:
        record = expire_record(session, record, actor_user_id=user.id, reason=payload.reason)
    except InvalidRecommendationStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _record_out(record)
