"""Unit tests for onboarding/jobs.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`) -- the job state-machine orchestration
glue. Uses two real, independent in-memory SQLite databases: one for
`identity`'s own ORM session (the job/review-item/artifact rows, bare
`sqlite:///:memory:`, same convention as
`tests/test_identity_repository_shares.py`) and a separate `StaticPool`
one standing in for the "target" database being onboarded (so discovery/
profiling/evaluation run real SQL, not mocks) -- exactly the pattern
already proven by this prompt's own manual end-to-end smoke test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from identity.models import Base, OnboardingJob, User
from identity.repositories.onboarding import (
    create_job,
    decide_review_item,
    get_latest_artifact,
    list_review_items,
    update_job_status,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from onboarding.jobs import (
    OnboardingJobError,
    cancel_job,
    publish_job,
    retry_job,
    run_discovery_stage,
)


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def target_engine() -> Iterator[Engine]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE Customers (Id INTEGER PRIMARY KEY, Email TEXT)"))
        conn.execute(
            text(
                "CREATE TABLE Orders ("
                "Id INTEGER PRIMARY KEY, CustomerId INTEGER, Amount REAL, "
                "FOREIGN KEY (CustomerId) REFERENCES Customers (Id))"
            )
        )
        for i in range(1, 6):
            conn.execute(
                text("INSERT INTO Customers VALUES (:i, :e)"), {"i": i, "e": f"c{i}@x.com"}
            )
        for i in range(1, 11):
            conn.execute(
                text("INSERT INTO Orders VALUES (:i, :c, :a)"),
                {"i": i, "c": (i % 5) + 1, "a": float(i) * 10},
            )
        conn.commit()
    yield engine
    engine.dispose()


def _job(session: Session) -> OnboardingJob:
    return create_job(
        session,
        tenant_id="tenant-a",
        created_by_user_id=None,
        database_label="Target",
        db_type="postgresql",
        db_host=None,
        db_port=None,
        db_name=None,
        db_user=None,
        db_schema=None,
    )


def _confirm_every_review_item(session: Session, job: OnboardingJob) -> None:
    user = User(email="sme@example.com", password_hash="x", display_name="sme")
    session.add(user)
    session.commit()
    session.refresh(user)
    for item in list_review_items(session, job.id):
        decide_review_item(session, item, decision="confirmed", decided_by_user_id=user.id)


class TestRunDiscoveryStageSuccess:
    def test_discovers_tables_relationships_pii_and_golden_questions(
        self, db_session: Session, target_engine: Engine
    ):
        job = _job(db_session)
        result = run_discovery_stage(db_session, job, target_engine)

        assert result.status == "awaiting_review"
        assert result.current_stage is None
        assert result.discovery_summary is not None
        assert result.discovery_summary["table_count"] == 2
        assert result.discovery_summary["view_count"] == 0
        assert result.discovery_summary["relationship_candidate_count"] >= 1
        assert result.discovery_summary["pii_finding_count"] >= 1
        assert result.discovery_summary["golden_question_count"] > 0

    def test_persists_one_review_item_per_candidate(
        self, db_session: Session, target_engine: Engine
    ):
        job = _job(db_session)
        run_discovery_stage(db_session, job, target_engine)

        items = list_review_items(db_session, job.id)
        assert len(items) > 0
        assert all(item.decision == "pending" for item in items)
        item_types = {item.item_type for item in items}
        assert "golden_question" in item_types
        assert "pii_classification" in item_types

    def test_opt_in_data_verification_flags_do_not_break_discovery(
        self, db_session: Session, target_engine: Engine
    ):
        job = _job(db_session)
        result = run_discovery_stage(
            db_session,
            job,
            target_engine,
            verify_relationships_with_data=True,
            verify_pii_with_data_flag=True,
        )
        assert result.status == "awaiting_review"


class TestRunDiscoveryStageFailure:
    def test_an_unexpected_failure_marks_the_job_failed_with_a_safe_message(
        self, db_session: Session, target_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ):
        def _boom(*args, **kwargs):
            raise RuntimeError("introspection exploded")

        monkeypatch.setattr("onboarding.jobs.introspect_schema", _boom)

        job = _job(db_session)
        result = run_discovery_stage(db_session, job, target_engine)

        assert result.status == "failed"
        assert result.current_stage == "discovery"
        assert result.error_message is not None
        assert "introspection exploded" in result.error_message


class TestPublishJobSuccess:
    def test_publish_produces_three_artifacts_and_marks_published(
        self, db_session: Session, target_engine: Engine
    ):
        job = _job(db_session)
        run_discovery_stage(db_session, job, target_engine)
        _confirm_every_review_item(db_session, job)

        result = publish_job(db_session, job, target_engine)

        assert result.status == "published"
        assert get_latest_artifact(db_session, job.id, "semantic_contract") is not None
        assert get_latest_artifact(db_session, job.id, "golden_questions") is not None
        evaluation = get_latest_artifact(db_session, job.id, "evaluation_report")
        assert evaluation is not None
        assert evaluation.content["total_count"] > 0
        assert evaluation.content["pass_count"] == evaluation.content["total_count"]


class TestPublishJobWithPendingItemsFails:
    def test_publishing_with_an_undecided_item_raises_and_marks_the_job_failed(
        self, db_session: Session, target_engine: Engine
    ):
        job = _job(db_session)
        run_discovery_stage(db_session, job, target_engine)
        # Deliberately leave every review item pending.

        with pytest.raises(OnboardingJobError):
            publish_job(db_session, job, target_engine)

        assert job.status == "failed"
        assert job.current_stage == "publish"
        assert job.error_message is not None
        assert "pending" in job.error_message


class TestCancelJob:
    def test_cancel_from_pending_succeeds(self, db_session: Session):
        job = _job(db_session)
        result = cancel_job(db_session, job)
        assert result.status == "cancelled"

    def test_cancel_a_published_job_raises(self, db_session: Session):
        job = _job(db_session)
        update_job_status(db_session, job, status="published")
        with pytest.raises(OnboardingJobError):
            cancel_job(db_session, job)

    def test_cancel_an_already_cancelled_job_raises(self, db_session: Session):
        job = _job(db_session)
        cancel_job(db_session, job)
        with pytest.raises(OnboardingJobError):
            cancel_job(db_session, job)


class TestRetryJob:
    def test_retry_a_discovery_failure_resets_to_pending_and_increments_retry_count(
        self, db_session: Session
    ):
        job = _job(db_session)
        update_job_status(db_session, job, status="failed", current_stage="discovery")

        result = retry_job(db_session, job)

        assert result.status == "pending"
        assert result.current_stage is None
        assert result.retry_count == 1

    def test_retry_a_publish_failure_resets_to_awaiting_review(self, db_session: Session):
        job = _job(db_session)
        update_job_status(db_session, job, status="failed", current_stage="publish")

        result = retry_job(db_session, job)

        assert result.status == "awaiting_review"

    def test_retry_a_non_failed_job_raises(self, db_session: Session):
        job = _job(db_session)
        with pytest.raises(OnboardingJobError):
            retry_job(db_session, job)
