"""Unit tests for identity/repositories/onboarding.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`) -- against a real in-memory SQLite
database, same convention as
tests/test_identity_repository_shares.py/test_identity_repository_history.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from identity.models import Base, User
from identity.repositories.onboarding import (
    add_artifact,
    add_review_items,
    create_job,
    decide_review_item,
    get_job_by_id,
    get_latest_artifact,
    get_review_item_by_id,
    increment_retry_count,
    list_artifacts,
    list_jobs_for_tenant,
    list_review_items,
    update_job_status,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _make_user(session: Session) -> User:
    user = User(email="reviewer@example.com", password_hash="x", display_name="reviewer")
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _create_job(session: Session, tenant_id: str = "tenant-a"):
    return create_job(
        session,
        tenant_id=tenant_id,
        created_by_user_id=None,
        database_label="My Warehouse",
        db_type="postgresql",
        db_host="db.internal",
        db_port=5432,
        db_name="warehouse",
        db_user="svc_account",
        db_schema="public",
    )


class TestCreateJob:
    def test_persists_non_secret_connection_fields_in_pending_status(self, db_session: Session):
        job = _create_job(db_session)
        assert job.status == "pending"
        assert job.db_host == "db.internal"
        assert job.db_user == "svc_account"
        assert job.version == 1
        assert job.retry_count == 0

    def test_never_accepts_a_password_parameter_at_all(self):
        """Structural guarantee, not a runtime check: `create_job` has no
        password/secret parameter in its signature for a caller to
        accidentally pass one into."""
        import inspect

        assert "password" not in inspect.signature(create_job).parameters
        assert "db_password" not in inspect.signature(create_job).parameters


class TestGetAndListJobs:
    def test_get_job_by_id_returns_none_for_a_missing_id(self, db_session: Session):
        assert get_job_by_id(db_session, uuid.uuid4()) is None

    def test_get_job_by_id_returns_the_created_job(self, db_session: Session):
        job = _create_job(db_session)
        fetched = get_job_by_id(db_session, job.id)
        assert fetched is not None
        assert fetched.id == job.id

    def test_list_jobs_for_tenant_only_returns_that_tenants_jobs(self, db_session: Session):
        job_a = _create_job(db_session, tenant_id="tenant-a")
        _create_job(db_session, tenant_id="tenant-b")

        jobs = list_jobs_for_tenant(db_session, "tenant-a")

        assert [j.id for j in jobs] == [job_a.id]


class TestUpdateJobStatus:
    def test_transition_to_failed_sets_error_message_and_bumps_version(self, db_session: Session):
        job = _create_job(db_session)
        update_job_status(db_session, job, status="failed", error_message="boom")
        assert job.status == "failed"
        assert job.error_message == "boom"
        assert job.version == 2

    def test_transition_to_a_non_failed_status_clears_a_stale_error(self, db_session: Session):
        job = _create_job(db_session)
        update_job_status(db_session, job, status="failed", error_message="boom")
        update_job_status(db_session, job, status="pending")
        assert job.error_message is None

    def test_discovery_summary_is_set_when_provided_and_kept_when_omitted(
        self, db_session: Session
    ):
        job = _create_job(db_session)
        update_job_status(
            db_session, job, status="awaiting_review", discovery_summary={"tables": 3}
        )
        assert job.discovery_summary == {"tables": 3}

        update_job_status(db_session, job, status="publishing")
        assert job.discovery_summary == {"tables": 3}


class TestIncrementRetryCount:
    def test_increments_from_zero(self, db_session: Session):
        job = _create_job(db_session)
        increment_retry_count(db_session, job)
        increment_retry_count(db_session, job)
        assert job.retry_count == 2


class TestReviewItems:
    def test_add_review_items_bulk_inserts_with_defaults(self, db_session: Session):
        job = _create_job(db_session)
        items = add_review_items(
            db_session,
            job,
            [
                {
                    "item_type": "pii_classification",
                    "table_name": "Customers",
                    "column_name": "Email",
                    "subject": "Customers.Email",
                    "payload": {"category": "email"},
                    "confidence": 0.85,
                },
                {
                    "item_type": "golden_question",
                    "subject": "How many rows?",
                    "payload": {"question": "How many rows?"},
                    "confidence": 0.9,
                    "is_ambiguous": True,
                },
            ],
        )
        assert len(items) == 2
        assert items[0].table_name == "Customers"
        assert items[0].is_ambiguous is False
        assert items[1].table_name is None
        assert items[1].is_ambiguous is True

    def test_get_review_item_by_id_returns_none_for_a_missing_id(self, db_session: Session):
        assert get_review_item_by_id(db_session, uuid.uuid4()) is None

    def test_list_review_items_filters_by_decision(self, db_session: Session):
        job = _create_job(db_session)
        items = add_review_items(
            db_session,
            job,
            [
                {
                    "item_type": "semantic_label",
                    "subject": "a",
                    "payload": {},
                    "confidence": 0.9,
                },
                {
                    "item_type": "semantic_label",
                    "subject": "b",
                    "payload": {},
                    "confidence": 0.9,
                },
            ],
        )
        user = _make_user(db_session)
        decide_review_item(db_session, items[0], decision="confirmed", decided_by_user_id=user.id)

        pending = list_review_items(db_session, job.id, decision="pending")
        confirmed = list_review_items(db_session, job.id, decision="confirmed")
        every_item = list_review_items(db_session, job.id)

        assert [i.subject for i in pending] == ["b"]
        assert [i.subject for i in confirmed] == ["a"]
        assert len(every_item) == 2

    def test_decide_review_item_records_decision_decider_and_notes(self, db_session: Session):
        job = _create_job(db_session)
        [item] = add_review_items(
            db_session,
            job,
            [{"item_type": "relationship", "subject": "a", "payload": {}, "confidence": 0.8}],
        )
        user = _make_user(db_session)

        decide_review_item(
            db_session,
            item,
            decision="rejected",
            decided_by_user_id=user.id,
            notes="not a real relationship",
        )

        assert item.decision == "rejected"
        assert item.decided_by_user_id == user.id
        assert item.decided_at is not None
        assert item.decision_notes == "not a real relationship"


class TestArtifacts:
    def test_add_artifact_starts_at_version_one(self, db_session: Session):
        job = _create_job(db_session)
        artifact = add_artifact(
            db_session, job, artifact_type="semantic_contract", content={"tables": []}
        )
        assert artifact.version == 1

    def test_add_artifact_increments_version_for_the_same_type_only(self, db_session: Session):
        job = _create_job(db_session)
        add_artifact(db_session, job, artifact_type="semantic_contract", content={"v": 1})
        second = add_artifact(db_session, job, artifact_type="semantic_contract", content={"v": 2})
        other_type = add_artifact(
            db_session, job, artifact_type="golden_questions", content={"v": 1}
        )

        assert second.version == 2
        assert other_type.version == 1

    def test_previous_versions_are_never_overwritten(self, db_session: Session):
        job = _create_job(db_session)
        add_artifact(db_session, job, artifact_type="semantic_contract", content={"v": 1})
        add_artifact(db_session, job, artifact_type="semantic_contract", content={"v": 2})

        all_versions = list_artifacts(db_session, job.id, artifact_type="semantic_contract")
        assert [a.content["v"] for a in all_versions] == [2, 1]  # newest first

    def test_get_latest_artifact_returns_the_highest_version(self, db_session: Session):
        job = _create_job(db_session)
        add_artifact(db_session, job, artifact_type="evaluation_report", content={"v": 1})
        add_artifact(db_session, job, artifact_type="evaluation_report", content={"v": 2})

        latest = get_latest_artifact(db_session, job.id, "evaluation_report")
        assert latest is not None
        assert latest.content == {"v": 2}

    def test_get_latest_artifact_returns_none_when_no_artifact_of_that_type_exists(
        self, db_session: Session
    ):
        job = _create_job(db_session)
        assert get_latest_artifact(db_session, job.id, "semantic_contract") is None
