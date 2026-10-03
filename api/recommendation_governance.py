"""The governed recommendation lifecycle's REST surface -- the API for
`identity/repositories/recommendation_governance.py`. Prompt 18
(`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`), extended by Prompt 31
(`31_RECOMMENDATION_ACTION_DASHBOARD_CONTRACT.md`) with notes, owner
assignment, owner/unassigned list filters, and structured logging on every
mutation.

**Every route here does exactly one authorization dance**: resolve the
`RecommendationRecord` row by id (never trusting a query filter alone to
scope it), then call `recommendation.governance_policy
.authorize_recommendation_action` and act only on an `allowed=True`
decision -- mirrors `api/semantic_catalog.py`'s own identical discipline.

**There is no create/upload route here.** A `RecommendationRecord` is
only ever created by the live `/ask` pipeline's own already-authorized
call (`api/recommendation_persistence.py`, itself gated by
`agent.authz.Permission.ASK` well before this module is ever reached) --
this surface is read + feedback + notes + ownership + lifecycle only,
never ingestion.

**No route here ever changes `recommendation.engine`'s own rules or
thresholds.** Master-contract requirement ("feedback must not
automatically alter production rules from a single event") holds
structurally: every write this module performs lands only in
`recommendation_records`/`recommendation_feedback_events`, which nothing
in `recommendation/engine.py` or `config/settings.py` ever reads.

**Every mutation is logged** through `security.audit_log.log_security_event`
(`event=recommendation_action`, carrying the record id, action, actor id and
status transition -- never the claim text, evidence, or note body) and
rate-limited through `api.rate_limit.enforce_api_action_rate_limit`.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from identity.models import RecommendationFeedbackEvent, RecommendationRecord, User
from identity.rbac import Permission
from identity.repositories.recommendation_governance import (
    InvalidRecommendationOwnerError,
    InvalidRecommendationStatusTransitionError,
    add_note,
    assign_owner,
    expire_record,
    get_record_by_id,
    list_feedback_events,
    list_records,
    mark_resolved,
    owner_display_names,
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
from api.rate_limit import enforce_api_action_rate_limit
from api.recommendation_governance_schemas import (
    AddNoteRequest,
    AssignOwnerRequest,
    ExpireRecommendationRequest,
    RecommendationFeedbackEventOut,
    RecommendationQualityMetricsOut,
    RecommendationRecordOut,
    ResolveRecommendationRequest,
    SubmitFeedbackRequest,
)
from config.settings import get_settings
from security.audit_log import log_security_event
from security.tenancy import resolve_actor_tenant_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/recommendations", tags=["recommendation-governance"])


def _not_found(detail: str = "Recommendation not found.") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


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


def _rate_limit(request: Request, action: str) -> None:
    enforce_api_action_rate_limit(request, action, get_settings())


def _record_out(
    record: RecommendationRecord, owner_display_name: str | None = None
) -> RecommendationRecordOut:
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
        owner_user_id=record.owner_user_id,
        owner_display_name=owner_display_name,
    )


def _event_out(event: RecommendationFeedbackEvent) -> RecommendationFeedbackEventOut:
    return RecommendationFeedbackEventOut(
        id=event.id,
        recommendation_id=event.recommendation_id,
        from_status=event.from_status,  # type: ignore[arg-type]
        to_status=event.to_status,  # type: ignore[arg-type]
        event_type=event.event_type,  # type: ignore[arg-type]
        actor_user_id=event.actor_user_id,
        actor_label=event.actor_label,
        reason=event.reason,
        detail=event.detail,
        recommendation_version=event.recommendation_version,
        evidence_version=event.evidence_version,
        created_at=event.created_at,
    )


def _log_action(
    user: User,
    record: RecommendationRecord,
    *,
    action: str,
    from_status: str | None = None,
    to_status: str | None = None,
) -> None:
    """One structured `security.audit` line per governed mutation. Carries
    ids, the action name, and the status transition only -- never the claim
    text, evidence, or a note/reason body, so the log stays safe to ship to
    a shared aggregator (master rule 7's "never log ... full result rows"
    spirit, applied to recommendation content)."""
    log_security_event(
        "recommendation_action",
        "info",
        f"recommendation {action}",
        action=action,
        record_id=str(record.id),
        actor_user_id=str(user.id),
        category=record.category,
        from_status=from_status,
        to_status=to_status,
    )


def _owner_names_for(session: Session, records: list[RecommendationRecord]) -> dict[uuid.UUID, str]:
    owner_ids = {record.owner_user_id for record in records if record.owner_user_id is not None}
    return owner_display_names(session, owner_ids)


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
    owner_user_id: str | None = Query(default=None),
    unassigned: bool = Query(default=False),
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[RecommendationRecordOut]:
    """Lists the caller's own tenant's recommendations. `owner_user_id` and
    `unassigned` are Prompt 31's ownership filters; `unassigned=true` takes
    precedence over an `owner_user_id` if both are given (they can't both be
    true of one row, so the combined filter would always be empty)."""
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
    owner_id: uuid.UUID | None = None
    if owner_user_id and not unassigned:
        try:
            owner_id = uuid.UUID(owner_user_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="owner_user_id must be a UUID.",
            ) from exc
    records = list_records(
        session,
        tenant_id,
        database_id=database_id,
        category=category,
        status=status_filter,
        owner_user_id=owner_id,
        unassigned=unassigned,
    )
    names = _owner_names_for(session, records)
    return [
        _record_out(record, names.get(record.owner_user_id) if record.owner_user_id else None)
        for record in records
    ]


@router.get("/{record_id}", response_model=RecommendationRecordOut)
def get_recommendation(
    record_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationRecordOut:
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.VIEW, record)
    names = _owner_names_for(session, [record])
    return _record_out(record, names.get(record.owner_user_id) if record.owner_user_id else None)


@router.get("/{record_id}/events", response_model=list[RecommendationFeedbackEventOut])
def get_recommendation_events(
    record_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[RecommendationFeedbackEventOut]:
    """The full, append-only audit trail for one recommendation -- verdicts,
    notes, and owner changes, oldest first."""
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.VIEW, record)
    events = list_feedback_events(session, record.id)
    return [_event_out(event) for event in events]


@router.post("/{record_id}/feedback", response_model=RecommendationRecordOut)
def submit_recommendation_feedback(
    record_id: uuid.UUID,
    payload: SubmitFeedbackRequest,
    request: Request,
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
    _rate_limit(request, "recommendation_feedback")
    from_status = record.status
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
    _log_action(
        user,
        record,
        action="feedback",
        from_status=from_status,
        to_status=record.status,
    )
    names = _owner_names_for(session, [record])
    return _record_out(record, names.get(record.owner_user_id) if record.owner_user_id else None)


@router.post("/{record_id}/resolve", response_model=RecommendationRecordOut)
def resolve_recommendation(
    record_id: uuid.UUID,
    payload: ResolveRecommendationRequest,
    request: Request,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationRecordOut:
    """`accepted`/`partially_useful` -> `resolved`."""
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.MARK_RESOLVED, record)
    _rate_limit(request, "recommendation_resolve")
    from_status = record.status
    try:
        record = mark_resolved(session, record, actor_user_id=user.id, reason=payload.reason)
    except InvalidRecommendationStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _log_action(user, record, action="resolve", from_status=from_status, to_status=record.status)
    names = _owner_names_for(session, [record])
    return _record_out(record, names.get(record.owner_user_id) if record.owner_user_id else None)


@router.post("/{record_id}/expire", response_model=RecommendationRecordOut)
def expire_recommendation(
    record_id: uuid.UUID,
    payload: ExpireRecommendationRequest,
    request: Request,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationRecordOut:
    """Any non-terminal status -> `expired` -- administrative override,
    `Permission.RECOMMENDATION_MANAGE` only (see
    `recommendation.governance_policy`'s own docstring)."""
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.EXPIRE, record)
    _rate_limit(request, "recommendation_expire")
    from_status = record.status
    try:
        record = expire_record(session, record, actor_user_id=user.id, reason=payload.reason)
    except InvalidRecommendationStatusTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _log_action(user, record, action="expire", from_status=from_status, to_status=record.status)
    names = _owner_names_for(session, [record])
    return _record_out(record, names.get(record.owner_user_id) if record.owner_user_id else None)


@router.post("/{record_id}/notes", response_model=RecommendationFeedbackEventOut)
def add_recommendation_note(
    record_id: uuid.UUID,
    payload: AddNoteRequest,
    request: Request,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationFeedbackEventOut:
    """Appends a note to the audit trail. Never changes the recommendation's
    status -- see `identity.repositories.recommendation_governance.add_note`."""
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.ADD_NOTE, record)
    _rate_limit(request, "recommendation_note")
    event = add_note(session, record, actor_user_id=user.id, note=payload.note)
    _log_action(
        user, record, action="note", from_status=event.from_status, to_status=event.to_status
    )
    return _event_out(event)


@router.post("/{record_id}/owner", response_model=RecommendationRecordOut)
def assign_recommendation_owner(
    record_id: uuid.UUID,
    payload: AssignOwnerRequest,
    request: Request,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> RecommendationRecordOut:
    """Sets or clears (`owner_user_id: null`) who is responsible for acting
    on this recommendation.

    An owner who is missing, disabled, in another tenant, or unable to review
    recommendations gets the **same** 404 -- so this route can't be used to
    probe whether a given user id exists in some other tenant."""
    user, session = user_and_session
    record = _require_record(session, record_id)
    _authorize(user, session, RecommendationAction.ASSIGN_OWNER, record)
    _rate_limit(request, "recommendation_owner")
    try:
        record = assign_owner(
            session,
            record,
            owner_user_id=payload.owner_user_id,
            actor_user_id=user.id,
        )
    except InvalidRecommendationOwnerError as exc:
        log_security_event(
            "recommendation_owner_rejected",
            "warning",
            "owner assignment refused",
            record_id=str(record.id),
            actor_user_id=str(user.id),
            requested_owner_user_id=str(payload.owner_user_id),
        )
        raise _not_found("Owner not found in this tenant.") from exc
    _log_action(
        user,
        record,
        action="assign_owner" if payload.owner_user_id else "clear_owner",
        from_status=record.status,
        to_status=record.status,
    )
    names = _owner_names_for(session, [record])
    return _record_out(record, names.get(record.owner_user_id) if record.owner_user_id else None)
