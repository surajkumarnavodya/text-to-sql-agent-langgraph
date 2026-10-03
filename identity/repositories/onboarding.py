"""Repository layer for the client-database onboarding engine --
`OnboardingJob`/`OnboardingReviewItem`/`OnboardingArtifact`, Prompt 08
(`08_ONBOARDING_ENGINE_CONTRACT.md`).

**This module never makes an authorization decision itself** -- every
function here is a plain data operation; `onboarding.policy
.authorize_onboarding_action` is the only place a caller may or may not
do something is decided, mirroring `identity/repositories/shares.py`'s
own identical split.

**Never persists a connection secret** -- see `identity.models
.OnboardingJob`'s own docstring. No function in this module accepts a
password/connection-secret parameter at all; there is nothing here to
accidentally write to a row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from identity.models import OnboardingArtifact, OnboardingJob, OnboardingReviewItem


def create_job(
    session: Session,
    *,
    tenant_id: str,
    created_by_user_id: uuid.UUID | None,
    database_label: str,
    db_type: str,
    db_host: str | None,
    db_port: int | None,
    db_name: str | None,
    db_user: str | None,
    db_schema: str | None,
) -> OnboardingJob:
    """Creates one `OnboardingJob` in `"pending"` status -- the connection
    was already tested (`db.connection.test_connection`) by the caller
    before this is called; this function only ever persists the
    non-secret connection fields, never the password used to test it."""
    job = OnboardingJob(
        tenant_id=tenant_id,
        created_by_user_id=created_by_user_id,
        database_label=database_label,
        db_type=db_type,
        db_host=db_host,
        db_port=db_port,
        db_name=db_name,
        db_user=db_user,
        db_schema=db_schema,
        status="pending",
    )
    session.add(job)
    session.commit()
    return job


def get_job_by_id(session: Session, job_id: uuid.UUID) -> OnboardingJob | None:
    return session.get(OnboardingJob, job_id)


def list_jobs_for_tenant(session: Session, tenant_id: str) -> list[OnboardingJob]:
    return list(
        session.scalars(
            select(OnboardingJob)
            .where(OnboardingJob.tenant_id == tenant_id)
            .order_by(OnboardingJob.created_at.desc())
        )
    )


def list_jobs_across_tenants(
    session: Session, *, status: str | None = None, limit: int = 500
) -> list[OnboardingJob]:
    """Every onboarding job, across every tenant -- Prompt 28
    (`28_GLOBAL_PLATFORM_ADMIN_DASHBOARD_CONTRACT.md`)'s own "jobs"
    dashboard section. See `identity.repositories.semantic_catalog
    .list_entries_across_tenants`'s own docstring for why this is a
    separate, differently-named function rather than an optional
    `tenant_id` on `list_jobs_for_tenant` above -- identical reasoning,
    same single caller (`api/platform_admin.py`, already
    `PLATFORM_ADMIN`-gated).
    """
    statement = select(OnboardingJob)
    if status is not None:
        statement = statement.where(OnboardingJob.status == status)
    statement = statement.order_by(OnboardingJob.created_at.desc()).limit(limit)
    return list(session.scalars(statement))


def count_pending_review_items_across_tenants(session: Session) -> dict[str, int]:
    """`{item_type: pending_count}` across every job in every tenant --
    the literal number this dashboard's "semantic review queue" section
    needs for onboarding's own half of that queue (the other half is
    `identity.repositories.semantic_catalog.list_entries_across_tenants`'s
    draft/reviewed counts). A plain grouped count, not a full row fetch,
    since the dashboard only needs the number here, not each item's own
    detail (an operator drills into a specific job's own review items via
    the existing `GET /onboarding/jobs/{id}/review-items` route instead).
    """
    rows = session.execute(
        select(OnboardingReviewItem.item_type, func.count())
        .where(OnboardingReviewItem.decision == "pending")
        .group_by(OnboardingReviewItem.item_type)
    ).all()
    return {item_type: count for item_type, count in rows}


def update_job_status(
    session: Session,
    job: OnboardingJob,
    *,
    status: str,
    current_stage: str | None = None,
    error_message: str | None = None,
    discovery_summary: dict[str, Any] | None = None,
) -> OnboardingJob:
    """Advances `job`'s own state -- always in place on the same row (a
    job has exactly one current status, unlike `OnboardingArtifact`,
    which keeps every version). `error_message` is cleared (set to
    `None`) on every transition to a non-`"failed"` status, so a stale
    error from a since-retried stage never lingers on a since-succeeded
    job."""
    job.status = status
    job.current_stage = current_stage  # type: ignore[assignment]
    job.error_message = error_message if status == "failed" else None  # type: ignore[assignment]
    if discovery_summary is not None:
        job.discovery_summary = discovery_summary
    job.version += 1
    session.commit()
    return job


def increment_retry_count(session: Session, job: OnboardingJob) -> OnboardingJob:
    job.retry_count += 1
    session.commit()
    return job


def add_review_items(
    session: Session, job: OnboardingJob, items: list[dict[str, Any]]
) -> list[OnboardingReviewItem]:
    """Bulk-inserts one `OnboardingReviewItem` per entry in `items`, each
    a plain dict with `item_type`/`table_name`/`column_name`/`subject`/
    `payload`/`confidence`/`is_ambiguous` keys (the shape
    `onboarding/jobs.py`'s own discovery-stage orchestration builds from
    every pipeline stage's typed dataclasses)."""
    rows = [
        OnboardingReviewItem(
            job_id=job.id,
            item_type=item["item_type"],
            table_name=item.get("table_name"),
            column_name=item.get("column_name"),
            subject=item["subject"],
            payload=item["payload"],
            confidence=item["confidence"],
            is_ambiguous=item.get("is_ambiguous", False),
        )
        for item in items
    ]
    session.add_all(rows)
    session.commit()
    return rows


def get_review_item_by_id(session: Session, item_id: uuid.UUID) -> OnboardingReviewItem | None:
    return session.get(OnboardingReviewItem, item_id)


def list_review_items(
    session: Session, job_id: uuid.UUID, decision: str | None = None
) -> list[OnboardingReviewItem]:
    stmt = select(OnboardingReviewItem).where(OnboardingReviewItem.job_id == job_id)
    if decision is not None:
        stmt = stmt.where(OnboardingReviewItem.decision == decision)
    return list(session.scalars(stmt.order_by(OnboardingReviewItem.created_at)))


def decide_review_item(
    session: Session,
    item: OnboardingReviewItem,
    *,
    decision: str,
    decided_by_user_id: uuid.UUID,
    notes: str | None = None,
) -> OnboardingReviewItem:
    """Records an SME's confirm/reject decision -- `decision` must be
    `"confirmed"` or `"rejected"` (the caller, `api/onboarding.py`,
    validates this via the request schema; this function trusts its
    caller the same way every other repository function in this
    codebase trusts its own validated input)."""
    item.decision = decision
    item.decided_by_user_id = decided_by_user_id
    item.decided_at = datetime.now(UTC)
    item.decision_notes = notes  # type: ignore[assignment]
    session.commit()
    return item


def add_artifact(
    session: Session,
    job: OnboardingJob,
    *,
    artifact_type: str,
    content: dict[str, Any],
) -> OnboardingArtifact:
    """Always inserts a **new** row, never updates one in place -- see
    `identity.models.OnboardingArtifact`'s own docstring for why a job's
    full artifact-revision history stays inspectable."""
    previous_versions = session.scalars(
        select(OnboardingArtifact.version)
        .where(OnboardingArtifact.job_id == job.id)
        .where(OnboardingArtifact.artifact_type == artifact_type)
        .order_by(OnboardingArtifact.version.desc())
    ).first()
    artifact = OnboardingArtifact(
        job_id=job.id,
        artifact_type=artifact_type,
        content=content,
        version=(previous_versions or 0) + 1,
    )
    session.add(artifact)
    session.commit()
    return artifact


def list_artifacts(
    session: Session, job_id: uuid.UUID, artifact_type: str | None = None
) -> list[OnboardingArtifact]:
    stmt = select(OnboardingArtifact).where(OnboardingArtifact.job_id == job_id)
    if artifact_type is not None:
        stmt = stmt.where(OnboardingArtifact.artifact_type == artifact_type)
    return list(session.scalars(stmt.order_by(OnboardingArtifact.version.desc())))


def get_latest_artifact(
    session: Session, job_id: uuid.UUID, artifact_type: str
) -> OnboardingArtifact | None:
    return session.scalars(
        select(OnboardingArtifact)
        .where(OnboardingArtifact.job_id == job_id)
        .where(OnboardingArtifact.artifact_type == artifact_type)
        .order_by(OnboardingArtifact.version.desc())
        .limit(1)
    ).first()
