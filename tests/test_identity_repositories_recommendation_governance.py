"""Unit tests for identity/repositories/recommendation_governance.py
(Prompt 18, `18_RECOMMENDATION_GOVERNANCE_CONTRACT.md`) -- against a real
in-memory SQLite database, same convention as
tests/test_identity_repositories_semantic_catalog.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from identity.models import Base
from identity.repositories.recommendation_governance import (
    InvalidRecommendationStatusTransitionError,
    compute_evidence_version,
    create_record,
    expire_record,
    get_record_by_id,
    list_feedback_events,
    list_records,
    mark_resolved,
    quality_metrics_for_tenant,
    submit_feedback,
)
from recommendation.governance import RecommendationStatus
from recommendation.models import Recommendation, RecommendationCategory, RecommendationKind
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agent.provenance import DataTruthLevel, ProvenancedClaim

_ACTOR = uuid.uuid4()
_OTHER_ACTOR = uuid.uuid4()


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _recommendation(**overrides) -> Recommendation:
    defaults = dict(
        kind=RecommendationKind.ACTION,
        claim=ProvenancedClaim(
            value="Investigate the anomalous spike.",
            level=DataTruthLevel.AI_INFERENCE,
            source="recommendation.engine.AnomalyRule",
        ),
        category=RecommendationCategory.ANOMALY,
        evidence=(
            ProvenancedClaim(
                value="Value at 2024 flagged anomalous.",
                level=DataTruthLevel.DATABASE_FACT,
                source="analytics.engine",
            ),
        ),
        affected_entity="2024",
        action="Investigate.",
        confidence=0.8,
        rule_or_model="recommendation.engine.AnomalyRule",
        limitations=("Statistical signal, not a confirmed cause.",),
    )
    defaults.update(overrides)
    return Recommendation(**defaults)


def _create(session: Session, *, tenant_id="tenant-a", database_id="hr", **overrides):
    recommendation = _recommendation(**overrides)
    return create_record(
        session,
        tenant_id=tenant_id,
        database_id=database_id,
        recommendation=recommendation,
        created_by_user_id=_ACTOR,
        source_question="why did sales spike?",
        source_sql="SELECT ...",
    )


class TestCreateRecord:
    def test_creates_in_generated_status(self, db_session: Session):
        record = _create(db_session)
        assert record.status == RecommendationStatus.GENERATED.value
        assert record.category == "anomaly"
        assert record.kind == "action"
        assert record.engine_version
        assert record.evidence_version
        assert record.evidence and record.evidence[0]["level"] == "database_fact"

    def test_records_an_initial_audit_event(self, db_session: Session):
        record = _create(db_session)
        events = list_feedback_events(db_session, record.id)
        assert len(events) == 1
        assert events[0].from_status is None
        assert events[0].to_status == RecommendationStatus.GENERATED.value
        assert events[0].actor_user_id == _ACTOR
        assert events[0].recommendation_version == record.engine_version
        assert events[0].evidence_version == record.evidence_version

    def test_system_actor_gets_a_label_when_no_user_id(self, db_session: Session):
        recommendation = _recommendation()
        record = create_record(
            db_session,
            tenant_id="tenant-a",
            database_id="hr",
            recommendation=recommendation,
            created_by_user_id=None,
        )
        events = list_feedback_events(db_session, record.id)
        assert events[0].actor_user_id is None
        assert events[0].actor_label == "system:recommendation_engine"

    def test_evidence_version_is_deterministic(self):
        evidence = [{"value": "x", "level": "database_fact", "source": "y", "grounded_in": []}]
        assert compute_evidence_version(evidence) == compute_evidence_version(evidence)

    def test_evidence_version_differs_for_different_evidence(self):
        evidence_a = [{"value": "x", "level": "database_fact"}]
        evidence_b = [{"value": "y", "level": "database_fact"}]
        assert compute_evidence_version(evidence_a) != compute_evidence_version(evidence_b)


class TestListAndGet:
    def test_get_record_by_id(self, db_session: Session):
        record = _create(db_session)
        fetched = get_record_by_id(db_session, record.id)
        assert fetched is not None
        assert fetched.id == record.id

    def test_get_nonexistent_record_returns_none(self, db_session: Session):
        assert get_record_by_id(db_session, uuid.uuid4()) is None

    def test_list_records_scoped_to_tenant(self, db_session: Session):
        _create(db_session, tenant_id="tenant-a")
        _create(db_session, tenant_id="tenant-b")
        records_a = list_records(db_session, "tenant-a")
        assert len(records_a) == 1

    def test_list_records_filters_by_category_and_status(self, db_session: Session):
        _create(db_session, category=RecommendationCategory.ANOMALY)
        _create(db_session, category=RecommendationCategory.DATA_QUALITY)
        anomaly_only = list_records(db_session, "tenant-a", category="anomaly")
        assert len(anomaly_only) == 1
        assert anomaly_only[0].category == "anomaly"


class TestSubmitFeedback:
    def test_generated_to_accepted(self, db_session: Session):
        record = _create(db_session)
        record = submit_feedback(
            db_session,
            record,
            target_status=RecommendationStatus.ACCEPTED,
            actor_user_id=_OTHER_ACTOR,
            reason="Looks correct.",
        )
        assert record.status == "accepted"

    def test_appends_a_new_event_without_losing_the_first(self, db_session: Session):
        record = _create(db_session)
        submit_feedback(
            db_session,
            record,
            target_status=RecommendationStatus.ACCEPTED,
            actor_user_id=_OTHER_ACTOR,
            reason="Looks correct.",
        )
        events = list_feedback_events(db_session, record.id)
        assert len(events) == 2
        assert events[1].from_status == "generated"
        assert events[1].to_status == "accepted"
        assert events[1].actor_user_id == _OTHER_ACTOR
        assert events[1].reason == "Looks correct."

    def test_invalid_transition_raises(self, db_session: Session):
        record = _create(db_session)
        with pytest.raises(InvalidRecommendationStatusTransitionError):
            submit_feedback(
                db_session,
                record,
                target_status=RecommendationStatus.RESOLVED,
                actor_user_id=_ACTOR,
            )

    def test_terminal_status_cannot_transition_further(self, db_session: Session):
        record = _create(db_session)
        submit_feedback(
            db_session, record, target_status=RecommendationStatus.REJECTED, actor_user_id=_ACTOR
        )
        with pytest.raises(InvalidRecommendationStatusTransitionError):
            submit_feedback(
                db_session,
                record,
                target_status=RecommendationStatus.ACCEPTED,
                actor_user_id=_ACTOR,
            )


class TestMarkResolvedAndExpire:
    def test_mark_resolved_from_accepted(self, db_session: Session):
        record = _create(db_session)
        submit_feedback(
            db_session, record, target_status=RecommendationStatus.ACCEPTED, actor_user_id=_ACTOR
        )
        record = mark_resolved(db_session, record, actor_user_id=_ACTOR, reason="Fixed.")
        assert record.status == "resolved"

    def test_mark_resolved_from_generated_raises(self, db_session: Session):
        record = _create(db_session)
        with pytest.raises(InvalidRecommendationStatusTransitionError):
            mark_resolved(db_session, record, actor_user_id=_ACTOR)

    def test_expire_allows_a_system_actor_with_no_user_id(self, db_session: Session):
        record = _create(db_session)
        record = expire_record(
            db_session,
            record,
            actor_user_id=None,
            actor_label="system:expiry_sweep",
            reason="Stale.",
        )
        assert record.status == "expired"
        events = list_feedback_events(db_session, record.id)
        assert events[-1].actor_label == "system:expiry_sweep"
        assert events[-1].actor_user_id is None

    def test_expire_from_accepted_also_allowed(self, db_session: Session):
        record = _create(db_session)
        submit_feedback(
            db_session, record, target_status=RecommendationStatus.ACCEPTED, actor_user_id=_ACTOR
        )
        record = expire_record(db_session, record, actor_user_id=_ACTOR)
        assert record.status == "expired"


class TestQualityMetrics:
    def test_counts_by_status_and_category(self, db_session: Session):
        a = _create(db_session, category=RecommendationCategory.ANOMALY)
        b = _create(db_session, category=RecommendationCategory.ANOMALY)
        _create(db_session, category=RecommendationCategory.DATA_QUALITY)
        submit_feedback(
            db_session, a, target_status=RecommendationStatus.ACCEPTED, actor_user_id=_ACTOR
        )
        submit_feedback(
            db_session, b, target_status=RecommendationStatus.REJECTED, actor_user_id=_ACTOR
        )

        metrics = quality_metrics_for_tenant(db_session, "tenant-a")

        assert metrics["total"] == 3
        assert metrics["by_status"]["accepted"] == 1
        assert metrics["by_status"]["rejected"] == 1
        assert metrics["by_status"]["generated"] == 1
        assert metrics["by_category"]["anomaly"]["accepted"] == 1
        assert metrics["by_category"]["anomaly"]["rejected"] == 1
        assert metrics["judged_total"] == 2
        assert metrics["acceptance_rate"] == pytest.approx(0.5)

    def test_acceptance_rate_is_none_when_nothing_judged_yet(self, db_session: Session):
        _create(db_session)
        metrics = quality_metrics_for_tenant(db_session, "tenant-a")
        assert metrics["judged_total"] == 0
        assert metrics["acceptance_rate"] is None

    def test_scoped_to_tenant_and_database(self, db_session: Session):
        _create(db_session, tenant_id="tenant-a", database_id="hr")
        _create(db_session, tenant_id="tenant-a", database_id="adventureworks")
        metrics = quality_metrics_for_tenant(db_session, "tenant-a", database_id="hr")
        assert metrics["total"] == 1

    def test_never_mutates_anything_it_reads(self, db_session: Session):
        """Master-contract requirement: a quality rollup must be a pure
        read -- it must never, itself, write back to a recommendation
        record or any engine setting."""
        record = _create(db_session)
        before = record.status
        quality_metrics_for_tenant(db_session, "tenant-a")
        refreshed = get_record_by_id(db_session, record.id)
        assert refreshed.status == before
