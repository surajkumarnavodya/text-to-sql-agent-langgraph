"""Client-database onboarding engine -- the REST surface for
`onboarding/jobs.py`'s pipeline. Prompt 08
(`08_ONBOARDING_ENGINE_CONTRACT.md`).

**Every route here does exactly one authorization dance**: resolve the
`OnboardingJob` row by id (never trusting a query filter alone to scope
it), then call `onboarding.policy.authorize_onboarding_action` and act
only on an `allowed=True` decision -- mirrors `api/shares.py`'s own
identical discipline.

**A connection secret (`db_password`) is accepted on exactly three
request bodies** (`CreateOnboardingJobRequest`/`RunDiscoveryRequest`/
`PublishJobRequest`) and is used immediately to build a throwaway
`Engine`, then discarded -- never passed to a repository function, never
logged, never part of any response. `POST /onboarding/jobs/{id}/discover`
and `.../publish` each build their own engine directly (bypassing
`db.connection`'s process-wide cached-engine pool on purpose: that cache's
own docstring says its key space is "the set of currently-configured
database connections... typically a handful," an assumption this dynamic,
potentially-many-distinct-candidate-databases-over-time engine would
violate) and `.dispose()` it in a `finally` block once the stage call
returns, so an onboarding attempt's connection pool never lingers past
the one request that needed it.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from identity.models import OnboardingJob, OnboardingReviewItem, User
from identity.rbac import Permission
from identity.repositories.onboarding import (
    create_job,
    get_job_by_id,
    get_review_item_by_id,
    list_artifacts,
    list_jobs_for_tenant,
    list_review_items,
)
from identity.repositories.onboarding import decide_review_item as _decide_review_item
from identity.repositories.users import get_user_permissions
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.identity_authz import require_local_user
from api.onboarding_schemas import (
    CreateOnboardingJobRequest,
    DecideReviewItemRequest,
    OnboardingArtifactOut,
    OnboardingJobOut,
    OnboardingReviewItemOut,
    PublishJobRequest,
    RunDiscoveryRequest,
)
from db.connection import DatabaseConnectionConfig, build_connection_url, test_connection
from onboarding.jobs import OnboardingJobError, cancel_job, publish_job, retry_job
from onboarding.jobs import run_discovery_stage as _run_discovery_stage
from onboarding.policy import OnboardingAction, authorize_onboarding_action
from security.tenancy import resolve_actor_tenant_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def _connection_config(
    *,
    db_type: str,
    db_host: str | None,
    db_port: int | None,
    db_name: str | None,
    db_user: str | None,
    db_password: str | None,
    db_schema: str | None,
) -> DatabaseConnectionConfig:
    return DatabaseConnectionConfig(
        name="onboarding",
        db_type=db_type,
        db_host=db_host,
        db_port=db_port,
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,  # type: ignore[arg-type]
        db_schema=db_schema,
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Onboarding job not found.")


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted.")


def _require_job(session: Session, job_id: uuid.UUID) -> OnboardingJob:
    job = get_job_by_id(session, job_id)
    if job is None:
        raise _not_found()
    return job


def _authorize(
    user: User, session: Session, action: OnboardingAction, job: OnboardingJob | None
) -> None:
    permissions = get_user_permissions(session, user.id)
    actor_tenant_id = resolve_actor_tenant_id(user)
    decision = authorize_onboarding_action(
        actor_permissions=permissions,
        actor_tenant_id=actor_tenant_id,
        job_tenant_id=job.tenant_id if job is not None else None,
        action=action,
    )
    if not decision.allowed:
        # A job that exists in a *different* tenant is denied the exact
        # same way as a genuinely nonexistent one -- see
        # `identity.share_policy`'s own precedent for this anti-
        # enumeration posture, reused here deliberately.
        if decision.reason == "cross_tenant":
            raise _not_found()
        raise _forbidden()


def _job_out(job: OnboardingJob) -> OnboardingJobOut:
    return OnboardingJobOut(
        id=job.id,
        database_label=job.database_label,
        db_type=job.db_type,
        db_host=job.db_host,
        db_port=job.db_port,
        db_name=job.db_name,
        db_user=job.db_user,
        db_schema=job.db_schema,
        status=job.status,  # type: ignore[arg-type]
        current_stage=job.current_stage,
        error_message=job.error_message,
        retry_count=job.retry_count,
        discovery_summary=job.discovery_summary,
        version=job.version,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _review_item_out(item: OnboardingReviewItem) -> OnboardingReviewItemOut:
    return OnboardingReviewItemOut(
        id=item.id,
        item_type=item.item_type,  # type: ignore[arg-type]
        table_name=item.table_name,
        column_name=item.column_name,
        subject=item.subject,
        payload=item.payload,
        confidence=item.confidence,
        is_ambiguous=item.is_ambiguous,
        decision=item.decision,  # type: ignore[arg-type]
        decided_at=item.decided_at,
        decision_notes=item.decision_notes,
        created_at=item.created_at,
    )


@router.post("/jobs", response_model=OnboardingJobOut)
def create_onboarding_job(
    payload: CreateOnboardingJobRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> OnboardingJobOut:
    """Tests the connection immediately (`db.connection.test_connection`)
    and creates the job only if it succeeds -- `db_password` is never
    written anywhere; only `test_connection`'s own pass/fail (and
    best-effort failure classification) ever leaves this function."""
    user, session = user_and_session
    _authorize(user, session, OnboardingAction.CREATE_JOB, job=None)

    config = _connection_config(
        db_type=payload.db_type,
        db_host=payload.db_host,
        db_port=payload.db_port,
        db_name=payload.db_name,
        db_user=payload.db_user,
        db_password=payload.db_password,
        db_schema=payload.db_schema,
    )
    result = test_connection(config)
    if not result.success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.message)

    tenant_id = resolve_actor_tenant_id(user)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not resolve a tenant for this account.",
        )
    job = create_job(
        session,
        tenant_id=tenant_id,
        created_by_user_id=user.id,
        database_label=payload.database_label,
        db_type=payload.db_type,
        db_host=payload.db_host,
        db_port=payload.db_port,
        db_name=payload.db_name,
        db_user=payload.db_user,
        db_schema=payload.db_schema,
    )
    return _job_out(job)


@router.get("/jobs", response_model=list[OnboardingJobOut])
def list_onboarding_jobs(
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[OnboardingJobOut]:
    user, session = user_and_session
    tenant_id = resolve_actor_tenant_id(user)
    if tenant_id is None:
        return []
    permissions = get_user_permissions(session, user.id)
    if not (
        Permission.ONBOARDING_MANAGE in permissions or Permission.ONBOARDING_REVIEW in permissions
    ):
        raise _forbidden()
    return [_job_out(job) for job in list_jobs_for_tenant(session, tenant_id)]


@router.get("/jobs/{job_id}", response_model=OnboardingJobOut)
def get_onboarding_job(
    job_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> OnboardingJobOut:
    user, session = user_and_session
    job = _require_job(session, job_id)
    _authorize(user, session, OnboardingAction.VIEW_JOB, job)
    return _job_out(job)


@router.post("/jobs/{job_id}/discover", response_model=OnboardingJobOut)
def run_discovery(
    job_id: uuid.UUID,
    payload: RunDiscoveryRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> OnboardingJobOut:
    user, session = user_and_session
    job = _require_job(session, job_id)
    _authorize(user, session, OnboardingAction.RUN_DISCOVERY, job)
    if job.status not in ("pending",):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot run discovery on a job in status {job.status!r}.",
        )

    config = _connection_config(
        db_type=job.db_type,
        db_host=job.db_host,
        db_port=job.db_port,
        db_name=job.db_name,
        db_user=job.db_user,
        db_password=payload.db_password,
        db_schema=job.db_schema,
    )
    engine = create_engine(build_connection_url(config), pool_pre_ping=True)
    try:
        job = _run_discovery_stage(
            session,
            job,
            engine,
            verify_relationships_with_data=payload.verify_relationships_with_data,
            verify_pii_with_data_flag=payload.verify_pii_with_data,
        )
    finally:
        engine.dispose()
    return _job_out(job)


@router.get("/jobs/{job_id}/review-items", response_model=list[OnboardingReviewItemOut])
def get_review_items(
    job_id: uuid.UUID,
    decision: str | None = None,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[OnboardingReviewItemOut]:
    user, session = user_and_session
    job = _require_job(session, job_id)
    _authorize(user, session, OnboardingAction.VIEW_JOB, job)
    return [
        _review_item_out(item) for item in list_review_items(session, job.id, decision=decision)
    ]


@router.post("/jobs/{job_id}/review-items/{item_id}/decide", response_model=OnboardingReviewItemOut)
def decide_review_item_route(
    job_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: DecideReviewItemRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> OnboardingReviewItemOut:
    user, session = user_and_session
    job = _require_job(session, job_id)
    _authorize(user, session, OnboardingAction.DECIDE_REVIEW_ITEM, job)

    item = get_review_item_by_id(session, item_id)
    if item is None or item.job_id != job.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review item not found.")

    item = _decide_review_item(
        session, item, decision=payload.decision, decided_by_user_id=user.id, notes=payload.notes
    )
    return _review_item_out(item)


@router.post("/jobs/{job_id}/publish", response_model=OnboardingJobOut)
def publish_onboarding_job(
    job_id: uuid.UUID,
    payload: PublishJobRequest,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> OnboardingJobOut:
    user, session = user_and_session
    job = _require_job(session, job_id)
    _authorize(user, session, OnboardingAction.PUBLISH, job)
    if job.status != "awaiting_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot publish a job in status {job.status!r}.",
        )

    config = _connection_config(
        db_type=job.db_type,
        db_host=job.db_host,
        db_port=job.db_port,
        db_name=job.db_name,
        db_user=job.db_user,
        db_password=payload.db_password,
        db_schema=job.db_schema,
    )
    engine = create_engine(build_connection_url(config), pool_pre_ping=True)
    try:
        try:
            job = publish_job(session, job, engine)
        except OnboardingJobError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    finally:
        engine.dispose()
    return _job_out(job)


@router.post("/jobs/{job_id}/cancel", response_model=OnboardingJobOut)
def cancel_onboarding_job(
    job_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> OnboardingJobOut:
    user, session = user_and_session
    job = _require_job(session, job_id)
    _authorize(user, session, OnboardingAction.CANCEL, job)
    try:
        job = cancel_job(session, job)
    except OnboardingJobError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _job_out(job)


@router.post("/jobs/{job_id}/retry", response_model=OnboardingJobOut)
def retry_onboarding_job(
    job_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> OnboardingJobOut:
    user, session = user_and_session
    job = _require_job(session, job_id)
    _authorize(user, session, OnboardingAction.RETRY, job)
    try:
        job = retry_job(session, job)
    except OnboardingJobError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _job_out(job)


@router.get("/jobs/{job_id}/artifacts", response_model=list[OnboardingArtifactOut])
def get_artifacts(
    job_id: uuid.UUID,
    user_and_session: tuple[User, Session] = Depends(require_local_user),
) -> list[OnboardingArtifactOut]:
    user, session = user_and_session
    job = _require_job(session, job_id)
    _authorize(user, session, OnboardingAction.VIEW_JOB, job)
    return [
        OnboardingArtifactOut(
            id=artifact.id,
            artifact_type=artifact.artifact_type,  # type: ignore[arg-type]
            content=artifact.content,
            version=artifact.version,
            created_at=artifact.created_at,
        )
        for artifact in list_artifacts(session, job.id)
    ]
