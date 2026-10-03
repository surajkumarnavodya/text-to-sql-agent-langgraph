"""Repository layer for the governed recommendation lifecycle --
`RecommendationRecord`/`RecommendationFeedbackEvent`, Prompt 18
(`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`), extended by Prompt 31
(`31_RECOMMENDATION_ACTION_DASHBOARD_CONTRACT.md`) with notes, owner
assignment, and race-safe status transitions.

**This module never makes an authorization decision itself** -- every
function here is a plain data operation; `recommendation.governance_policy
.authorize_recommendation_action` is the only place a caller may or may
not do something is decided, mirroring `identity/repositories
/semantic_catalog.py`'s own identical split.

**Status transitions are defended here, not only at the API layer.**
`_assert_transition_valid` checks the legal transition table against this
session's own, possibly stale, loaded status. That alone is not enough: two
reviewers acting on the same record at once can both pass it. So every
status change is one conditional `UPDATE ... WHERE id = :id AND status =
:expected`, and only the caller whose `UPDATE` actually matched a row
succeeds -- the same race fix `identity/repositories/semantic_catalog
._apply_transition` applies to the semantic catalog (Prompt 27), written
here against this table's own columns.

**Notes and owner changes never touch `status`.** They append a
`RecommendationFeedbackEvent` with `event_type` `note` / `owner_assigned`,
so the one append-only audit trail records every governed action.

**No function in this module ever reads `RecommendationFeedbackEvent`
rows to change `recommendation.engine`'s own behavior** -- see
`identity.models.RecommendationFeedbackEvent`'s own docstring. The quality
rollup (`quality_metrics_for_tenant`) is the only read path that aggregates
these rows, and it returns counts for a human to look at.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from recommendation.governance import VALID_STATUS_TRANSITIONS, RecommendationStatus
from recommendation.models import Recommendation
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from identity.models import RecommendationFeedbackEvent, RecommendationRecord, User
from identity.rbac import Permission
from identity.repositories.users import get_user_permissions


class InvalidRecommendationStatusTransitionError(Exception):
    """Raised when a caller attempts a status transition
    `recommendation.governance.VALID_STATUS_TRANSITIONS` doesn't allow
    from the record's current status (e.g. resolving a still-`generated`
    record, transitioning out of a terminal status), or when a concurrent
    caller already moved the record (the conditional `UPDATE` matched no
    row)."""


class InvalidRecommendationOwnerError(Exception):
    """Raised when an owner assignment names a user who is not an active
    account in the record's own tenant. Callers map this to the same
    not-found response an unknown user would get -- a cross-tenant user id
    must never be distinguishable from a nonexistent one."""


def _assert_transition_valid(record: RecommendationRecord, target: RecommendationStatus) -> None:
    current = RecommendationStatus(record.status)
    if target not in VALID_STATUS_TRANSITIONS.get(current, frozenset()):
        raise InvalidRecommendationStatusTransitionError(
            f"Cannot move a {current.value!r} recommendation to {target.value!r}."
        )


def compute_evidence_version(evidence: list[dict[str, Any]]) -> str:
    """A deterministic, content-addressable identifier for one
    recommendation's own evidence snapshot -- SHA-256 over the evidence's
    canonical JSON encoding (sorted keys, no whitespace variance), so the
    identical evidence always produces the identical version string
    regardless of dict key order. Deliberately independent of
    `Recommendation.engine_version` (see `RecommendationRecord`'s own
    docstring for why the two are separate, both-captured identifiers).
    """
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_record(
    session: Session,
    *,
    tenant_id: str,
    database_id: str,
    recommendation: Recommendation,
    created_by_user_id: uuid.UUID | None,
    source_question: str | None = None,
    source_sql: str | None = None,
) -> RecommendationRecord:
    """Persists one already-computed `recommendation.models.Recommendation`
    (Prompt 17's engine output) as a new `RecommendationRecord` in
    `"generated"` status, and records the initial
    `RecommendationFeedbackEvent` (`from_status=None`,
    `to_status="generated"`) that establishes the audit trail's own
    starting point.

    This is the *only* place a `RecommendationRecord` is ever created --
    there is no API route for it (see `recommendation.governance_policy`'s
    own docstring): a record only ever comes from the live `/ask`
    pipeline's own already-authorized call (`api/recommendation_persistence
    .py`), never from a caller hitting this governance surface directly.
    """
    evidence = [item.model_dump(mode="json") for item in recommendation.evidence]
    evidence_version = compute_evidence_version(evidence)

    record = RecommendationRecord(
        tenant_id=tenant_id,
        database_id=database_id,
        category=recommendation.category.value if recommendation.category else None,
        kind=recommendation.kind.value,
        rule_or_model=recommendation.rule_or_model,
        claim_text=recommendation.claim.value,
        rationale=recommendation.rationale,
        affected_entity=recommendation.affected_entity,
        action=recommendation.action,
        measurable_impact=recommendation.measurable_impact,
        confidence=recommendation.confidence,
        evidence=evidence,
        limitations=list(recommendation.limitations),
        engine_version=recommendation.engine_version,
        evidence_version=evidence_version,
        status=RecommendationStatus.GENERATED.value,
        generated_at=datetime.fromisoformat(recommendation.generated_at),
        source_question=source_question,
        source_sql=source_sql,
        created_by_user_id=created_by_user_id,
    )
    session.add(record)
    session.flush()

    event = RecommendationFeedbackEvent(
        recommendation_id=record.id,
        from_status=None,
        to_status=RecommendationStatus.GENERATED.value,
        event_type="status_change",
        actor_user_id=created_by_user_id,
        actor_label=None if created_by_user_id is not None else "system:recommendation_engine",
        reason="Generated by recommendation.engine.generate_recommendations.",
        recommendation_version=record.engine_version,
        evidence_version=record.evidence_version,
    )
    session.add(event)
    session.commit()
    return record


def get_record_by_id(session: Session, record_id: uuid.UUID) -> RecommendationRecord | None:
    return session.get(RecommendationRecord, record_id)


def list_records(
    session: Session,
    tenant_id: str,
    *,
    database_id: str | None = None,
    category: str | None = None,
    status: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    unassigned: bool = False,
    limit: int = 200,
) -> list[RecommendationRecord]:
    stmt = select(RecommendationRecord).where(RecommendationRecord.tenant_id == tenant_id)
    if database_id is not None:
        stmt = stmt.where(RecommendationRecord.database_id == database_id)
    if category is not None:
        stmt = stmt.where(RecommendationRecord.category == category)
    if status is not None:
        stmt = stmt.where(RecommendationRecord.status == status)
    if owner_user_id is not None:
        stmt = stmt.where(RecommendationRecord.owner_user_id == owner_user_id)
    if unassigned:
        stmt = stmt.where(RecommendationRecord.owner_user_id.is_(None))
    stmt = stmt.order_by(RecommendationRecord.created_at.desc()).limit(limit)
    return list(session.scalars(stmt))


def list_feedback_events(
    session: Session, recommendation_id: uuid.UUID
) -> list[RecommendationFeedbackEvent]:
    """Every feedback/lifecycle event for one record, oldest first -- the
    full, append-only audit trail `GET /recommendations/{id}/events`
    reports."""
    stmt = (
        select(RecommendationFeedbackEvent)
        .where(RecommendationFeedbackEvent.recommendation_id == recommendation_id)
        .order_by(RecommendationFeedbackEvent.created_at.asc())
    )
    return list(session.scalars(stmt))


def owner_display_names(session: Session, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Display names for a batch of owner ids in one query -- used so a list
    response can label owners without one lookup per row. A user with no
    display name is simply absent from the result (the caller shows nothing,
    never a fabricated name or an email address)."""
    if not user_ids:
        return {}
    rows = session.execute(select(User.id, User.display_name).where(User.id.in_(user_ids))).all()
    return {user_id: name for user_id, name in rows if name}


def _apply_conditional_status_update(
    session: Session,
    record: RecommendationRecord,
    *,
    expected_status: str,
    new_status: str,
) -> None:
    """Atomically moves `record` from `expected_status` to `new_status`.

    Raises:
        InvalidRecommendationStatusTransitionError: if the row's committed
            status is no longer `expected_status` (rowcount 0) -- another
            caller's transition already landed. The session is rolled back
            so nothing from this attempt is written.
    """
    result = session.execute(
        sa_update(RecommendationRecord)
        .where(RecommendationRecord.id == record.id)
        .where(RecommendationRecord.status == expected_status)
        .values(status=new_status, updated_at=datetime.now(UTC))
    )
    if result.rowcount == 0:
        session.rollback()
        raise InvalidRecommendationStatusTransitionError(
            f"Recommendation {record.id} is no longer {expected_status!r} -- another "
            "decision was already recorded."
        )


def submit_feedback(
    session: Session,
    record: RecommendationRecord,
    *,
    target_status: RecommendationStatus,
    actor_user_id: uuid.UUID | None,
    actor_label: str | None = None,
    reason: str | None = None,
) -> RecommendationRecord:
    """Transitions `record` to `target_status`, appending the
    `RecommendationFeedbackEvent` that records this single decision --
    one event per call, never a batch, so each is independently
    attributable to one actor/reason/timestamp (master rule 11's
    "explicit errors, versioning, idempotency, structured observability").

    The status change itself is a conditional `UPDATE` (see
    `_apply_conditional_status_update`), so two concurrent callers can never
    both report success for the same transition.

    Raises:
        InvalidRecommendationStatusTransitionError: if `record.status`
            can't legally move to `target_status`, or a concurrent caller
            already moved it -- see `recommendation.governance`.
    """
    _assert_transition_valid(record, target_status)
    from_status = record.status
    _apply_conditional_status_update(
        session,
        record,
        expected_status=from_status,
        new_status=target_status.value,
    )

    event = RecommendationFeedbackEvent(
        recommendation_id=record.id,
        from_status=from_status,
        to_status=target_status.value,
        event_type="status_change",
        actor_user_id=actor_user_id,
        actor_label=actor_label if actor_user_id is None else None,
        reason=reason,
        recommendation_version=record.engine_version,
        evidence_version=record.evidence_version,
    )
    session.add(event)
    session.commit()
    session.refresh(record)
    return record


def mark_resolved(
    session: Session,
    record: RecommendationRecord,
    *,
    actor_user_id: uuid.UUID | None,
    reason: str | None = None,
) -> RecommendationRecord:
    """`ACCEPTED`/`PARTIALLY_USEFUL` -> `RESOLVED`: the underlying issue
    this recommendation flagged has since been addressed in the real
    world. A thin, named convenience over `submit_feedback` -- not a
    separate code path -- so this specific, common action reads clearly
    at call sites without duplicating the transition/audit logic."""
    return submit_feedback(
        session,
        record,
        target_status=RecommendationStatus.RESOLVED,
        actor_user_id=actor_user_id,
        reason=reason,
    )


def expire_record(
    session: Session,
    record: RecommendationRecord,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_label: str | None = None,
    reason: str | None = None,
) -> RecommendationRecord:
    """Any non-terminal status -> `EXPIRED`. Unlike every other transition
    here, `actor_user_id` may legitimately be `None` with a real
    `actor_label` (e.g. `"system:expiry_sweep"`) -- an administrative
    expiry is the one transition this codebase anticipates a future
    automated job performing, not only a human (see `identity.models
    .RecommendationFeedbackEvent`'s own docstring for the actor_user_id/
    actor_label fallback convention this relies on)."""
    return submit_feedback(
        session,
        record,
        target_status=RecommendationStatus.EXPIRED,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        reason=reason,
    )


def add_note(
    session: Session,
    record: RecommendationRecord,
    *,
    actor_user_id: uuid.UUID,
    note: str,
) -> RecommendationFeedbackEvent:
    """Appends a free-text note to `record`'s audit trail. Never changes
    `status` -- a note is commentary on a recommendation, not a verdict.

    `from_status`/`to_status` both record the status as it is at the moment
    the note is written, so a note remains interpretable in the timeline
    even after the record has since moved on.
    """
    session.refresh(record)
    event = RecommendationFeedbackEvent(
        recommendation_id=record.id,
        from_status=record.status,
        to_status=record.status,
        event_type="note",
        actor_user_id=actor_user_id,
        actor_label=None,
        reason=note,
        recommendation_version=record.engine_version,
        evidence_version=record.evidence_version,
    )
    session.add(event)
    session.commit()
    return event


def assign_owner(
    session: Session,
    record: RecommendationRecord,
    *,
    owner_user_id: uuid.UUID | None,
    actor_user_id: uuid.UUID,
) -> RecommendationRecord:
    """Sets (or, with `owner_user_id=None`, clears) the owner of `record`,
    appending one `owner_assigned` event that names both the new and the
    previous owner.

    The owner must be an **active** user whose `tenant_id` equals the
    record's own, and who holds a recommendation-review permission (so the
    person named is actually able to act on the item) -- all checked here,
    not only in the API layer, so no caller can assign a cross-tenant,
    disabled, or non-reviewer account.

    Raises:
        InvalidRecommendationOwnerError: the named user doesn't exist, isn't
            active, belongs to another tenant, or can't review recommendations.
    """
    if owner_user_id is not None:
        owner = session.get(User, owner_user_id)
        if owner is None or owner.status != "active" or owner.tenant_id != record.tenant_id:
            raise InvalidRecommendationOwnerError("Owner is not an active user in this tenant.")
        owner_permissions = get_user_permissions(session, owner.id)
        if not (
            Permission.RECOMMENDATION_REVIEW in owner_permissions
            or Permission.RECOMMENDATION_MANAGE in owner_permissions
        ):
            raise InvalidRecommendationOwnerError("Owner cannot review recommendations.")

    session.refresh(record)
    previous_owner = record.owner_user_id
    record.owner_user_id = owner_user_id
    record.updated_at = datetime.now(UTC)
    event = RecommendationFeedbackEvent(
        recommendation_id=record.id,
        from_status=record.status,
        to_status=record.status,
        event_type="owner_assigned",
        actor_user_id=actor_user_id,
        actor_label=None,
        reason=None,
        detail={
            "owner_user_id": str(owner_user_id) if owner_user_id else None,
            "previous_owner_user_id": str(previous_owner) if previous_owner else None,
        },
        recommendation_version=record.engine_version,
        evidence_version=record.evidence_version,
    )
    session.add(event)
    session.commit()
    session.refresh(record)
    return record


def quality_metrics_for_tenant(
    session: Session, tenant_id: str, *, database_id: str | None = None
) -> dict[str, Any]:
    """A read-only aggregate rollup of recommendation outcomes for a
    future human-facing dashboard -- the literal "recommendation quality
    can be measured" acceptance criterion, satisfied as plain counts, not
    a derived score this module invents an opinion about. Grouped by
    `status` and by `(category, status)` so a caller can see both the
    overall funnel (how many are still `generated` vs. judged) and which
    categories are producing useful vs. incorrect/rejected output.

    Never used by anything that changes `recommendation.engine`'s own
    behavior -- see this module's own docstring.
    """
    stmt = select(RecommendationRecord.status, func.count()).where(
        RecommendationRecord.tenant_id == tenant_id
    )
    if database_id is not None:
        stmt = stmt.where(RecommendationRecord.database_id == database_id)
    status_counts: dict[str, int] = dict(
        session.execute(stmt.group_by(RecommendationRecord.status)).all()  # type: ignore[arg-type]
    )

    by_category_stmt = select(
        RecommendationRecord.category, RecommendationRecord.status, func.count()
    ).where(RecommendationRecord.tenant_id == tenant_id)
    if database_id is not None:
        by_category_stmt = by_category_stmt.where(RecommendationRecord.database_id == database_id)
    by_category_rows = session.execute(
        by_category_stmt.group_by(RecommendationRecord.category, RecommendationRecord.status)
    ).all()

    by_category: dict[str, dict[str, int]] = {}
    for category, status, count in by_category_rows:
        key = category or "uncategorized"
        by_category.setdefault(key, {})[status] = count

    total = sum(status_counts.values())
    judged_positive = sum(
        status_counts.get(s, 0)
        for s in (
            RecommendationStatus.ACCEPTED.value,
            RecommendationStatus.PARTIALLY_USEFUL.value,
            RecommendationStatus.RESOLVED.value,
        )
    )
    judged_negative = sum(
        status_counts.get(s, 0)
        for s in (RecommendationStatus.REJECTED.value, RecommendationStatus.INCORRECT.value)
    )
    judged_total = judged_positive + judged_negative
    return {
        "total": total,
        "by_status": status_counts,
        "by_category": by_category,
        "judged_total": judged_total,
        # None (not 0.0) when nothing has been judged yet -- a 0% "quality"
        # score with zero sample size would be misleading, not just
        # uninformative.
        "acceptance_rate": (judged_positive / judged_total) if judged_total else None,
    }
