"""Repository layer for the governed recommendation lifecycle --
`RecommendationRecord`/`RecommendationFeedbackEvent`, Prompt 18
(`18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`).

**This module never makes an authorization decision itself** -- every
function here is a plain data operation; `recommendation.governance_policy
.authorize_recommendation_action` is the only place a caller may or may
not do something is decided, mirroring `identity/repositories
/semantic_catalog.py`'s own identical split.

**Status transitions are defended here, not only at the API layer** --
the exact same defense-in-depth posture `identity/repositories
/semantic_catalog.py::_assert_transition_valid` already establishes:
`_assert_transition_valid` raises `InvalidRecommendationStatusTransitionError`
against `recommendation.governance.VALID_STATUS_TRANSITIONS` regardless
of whatever `api/recommendation_governance.py`'s own pre-flight check
already did.

**No function in this module ever reads `RecommendationFeedbackEvent`
rows to change `recommendation.engine`'s own behavior** -- see
`identity.models.RecommendationFeedbackEvent`'s own docstring for why
that is a structural, not just documented, guarantee (master-contract
requirement: "feedback must not automatically alter production rules
from a single event"). `quality_metrics_for_tenant` is the one read path
over this data, and it only ever returns counts for a human to look at.
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
from sqlalchemy.orm import Session

from identity.models import RecommendationFeedbackEvent, RecommendationRecord


class InvalidRecommendationStatusTransitionError(Exception):
    """Raised when a caller attempts a status transition
    `recommendation.governance.VALID_STATUS_TRANSITIONS` doesn't allow
    from the record's current status (e.g. resolving a still-`generated`
    record, or transitioning out of a terminal status)."""


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
    limit: int = 200,
) -> list[RecommendationRecord]:
    stmt = select(RecommendationRecord).where(RecommendationRecord.tenant_id == tenant_id)
    if database_id is not None:
        stmt = stmt.where(RecommendationRecord.database_id == database_id)
    if category is not None:
        stmt = stmt.where(RecommendationRecord.category == category)
    if status is not None:
        stmt = stmt.where(RecommendationRecord.status == status)
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

    Raises:
        InvalidRecommendationStatusTransitionError: if `record.status`
            can't legally move to `target_status` -- see
            `recommendation.governance.VALID_STATUS_TRANSITIONS`.
    """
    _assert_transition_valid(record, target_status)
    from_status = record.status
    record.status = target_status.value
    record.updated_at = datetime.now(UTC)

    event = RecommendationFeedbackEvent(
        recommendation_id=record.id,
        from_status=from_status,
        to_status=target_status.value,
        actor_user_id=actor_user_id,
        actor_label=actor_label if actor_user_id is None else None,
        reason=reason,
        recommendation_version=record.engine_version,
        evidence_version=record.evidence_version,
    )
    session.add(event)
    session.commit()
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
