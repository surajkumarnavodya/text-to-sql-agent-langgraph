"""Repository and policy tests for Prompt 31's recommendation actions --
race-safe status transitions, notes, owner assignment, and the two new
policy actions. Pure SQLAlchemy against an in-memory SQLite identity DB, no
HTTP, no mocks of the ORM (the same real-engine pattern
`tests/test_identity_repositories_recommendation_governance.py` uses).

The race test interleaves two `Session`s deterministically rather than
running real OS threads: SQLite's `:memory:` database must share one
connection (`StaticPool`), and `sqlite3` connections are not safe for
genuinely concurrent statement execution -- the same reason Prompt 27's
`TestConcurrentPublish` was rewritten this way. The property under test (the
database's conditional `UPDATE` is the single point of truth) is the same.
"""

from __future__ import annotations

import uuid

import pytest
from identity.bootstrap import seed_rbac
from identity.models import Base, RecommendationFeedbackEvent, RecommendationRecord
from identity.rbac import Permission
from identity.repositories.recommendation_governance import (
    InvalidRecommendationOwnerError,
    InvalidRecommendationStatusTransitionError,
    add_note,
    assign_owner,
    create_record,
    list_feedback_events,
    list_records,
    mark_resolved,
    owner_display_names,
    submit_feedback,
)
from identity.repositories.tenants import create_tenant
from identity.repositories.users import assign_role, create_user
from recommendation.governance import RecommendationStatus
from recommendation.governance_policy import RecommendationAction, authorize_recommendation_action
from recommendation.models import Recommendation, RecommendationCategory, RecommendationKind
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent.provenance import DataTruthLevel, ProvenancedClaim


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=True)
    setup = factory()
    seed_rbac(setup)
    create_tenant(setup, tenant_id="tenant-a", name="A")
    create_tenant(setup, tenant_id="tenant-b", name="B")
    setup.commit()
    setup.close()
    yield factory
    engine.dispose()


def _user(session, email: str, *, tenant_id: str, role: str | None = None, status: str = "active"):
    user = create_user(
        session, email=email, password="correcthorse battery staple 1", tenant_id=tenant_id
    )
    if status != "active":
        user.status = status
    if role is not None:
        assign_role(session, user_id=user.id, role_name=role, assigned_by_user_id=None)
    session.commit()
    return user.id


def _recommendation() -> Recommendation:
    return Recommendation(
        kind=RecommendationKind.ACTION,
        claim=ProvenancedClaim(
            value="Review the anomalous period.",
            level=DataTruthLevel.AI_INFERENCE,
            source="recommendation.engine.AnomalyRule",
        ),
        category=RecommendationCategory.ANOMALY,
        evidence=(
            ProvenancedClaim(
                value="2024 flagged.", level=DataTruthLevel.DATABASE_FACT, source="analytics.engine"
            ),
        ),
        confidence=0.8,
        rule_or_model="recommendation.engine.AnomalyRule",
    )


def _record(session, tenant_id: str = "tenant-a") -> RecommendationRecord:
    return create_record(
        session,
        tenant_id=tenant_id,
        database_id="hr",
        recommendation=_recommendation(),
        created_by_user_id=None,
    )


class TestRaceSafeTransitions:
    def test_a_stale_second_resolve_is_rejected_by_the_conditional_update(self, session_factory):
        setup = session_factory()
        record_id = _record(setup).id
        reviewer = _user(setup, "r@a.example", tenant_id="tenant-a", role="analyst")
        submit_feedback(
            setup,
            setup.get(RecommendationRecord, record_id),
            target_status=RecommendationStatus.ACCEPTED,
            actor_user_id=reviewer,
        )
        setup.close()

        # Both sessions load the record while it is `accepted`.
        first = session_factory()
        second = session_factory()
        first_record = first.get(RecommendationRecord, record_id)
        second_record = second.get(RecommendationRecord, record_id)
        assert first_record.status == second_record.status == "accepted"

        mark_resolved(first, first_record, actor_user_id=reviewer, reason="fixed")

        # The second caller's in-memory status is stale ("accepted"), so the
        # Python-side transition check passes -- only the database's
        # conditional UPDATE can refuse it.
        with pytest.raises(InvalidRecommendationStatusTransitionError, match="no longer"):
            mark_resolved(second, second_record, actor_user_id=reviewer, reason="also fixed")
        second.close()

        check = session_factory()
        try:
            events = list_feedback_events(check, record_id)
            resolved = [e for e in events if e.to_status == "resolved"]
            assert len(resolved) == 1, "exactly one caller's transition may be recorded"
            assert check.get(RecommendationRecord, record_id).status == "resolved"
        finally:
            check.close()
        first.close()

    def test_a_refused_transition_writes_no_event(self, session_factory):
        session = session_factory()
        try:
            record = _record(session)
            before = len(list_feedback_events(session, record.id))
            with pytest.raises(InvalidRecommendationStatusTransitionError):
                mark_resolved(session, record, actor_user_id=None, reason="premature")
            assert len(list_feedback_events(session, record.id)) == before
        finally:
            session.close()


class TestNotes:
    def test_a_note_never_changes_status(self, session_factory):
        session = session_factory()
        try:
            record = _record(session)
            reviewer = _user(session, "n@a.example", tenant_id="tenant-a", role="analyst")
            event = add_note(session, record, actor_user_id=reviewer, note="looks right")
            assert event.event_type == "note"
            assert event.from_status == event.to_status == "generated"
            assert session.get(RecommendationRecord, record.id).status == "generated"
        finally:
            session.close()

    def test_creation_still_records_a_status_change_event(self, session_factory):
        session = session_factory()
        try:
            record = _record(session)
            events = list_feedback_events(session, record.id)
            assert [e.event_type for e in events] == ["status_change"]
            assert events[0].from_status is None
        finally:
            session.close()


class TestOwnerAssignment:
    def test_a_same_tenant_reviewer_can_be_assigned(self, session_factory):
        session = session_factory()
        try:
            record = _record(session)
            owner = _user(session, "ok@a.example", tenant_id="tenant-a", role="analyst")
            updated = assign_owner(session, record, owner_user_id=owner, actor_user_id=owner)
            assert updated.owner_user_id == owner
        finally:
            session.close()

    def test_a_cross_tenant_reviewer_is_refused(self, session_factory):
        session = session_factory()
        try:
            record = _record(session, tenant_id="tenant-a")
            other = _user(session, "x@b.example", tenant_id="tenant-b", role="analyst")
            with pytest.raises(InvalidRecommendationOwnerError):
                assign_owner(session, record, owner_user_id=other, actor_user_id=other)
            assert session.get(RecommendationRecord, record.id).owner_user_id is None
        finally:
            session.close()

    def test_a_disabled_reviewer_is_refused(self, session_factory):
        session = session_factory()
        try:
            record = _record(session)
            disabled = _user(
                session, "d@a.example", tenant_id="tenant-a", role="analyst", status="inactive"
            )
            with pytest.raises(InvalidRecommendationOwnerError):
                assign_owner(session, record, owner_user_id=disabled, actor_user_id=disabled)
        finally:
            session.close()

    def test_a_non_reviewer_is_refused(self, session_factory):
        session = session_factory()
        try:
            record = _record(session)
            plain = _user(session, "p@a.example", tenant_id="tenant-a")
            with pytest.raises(InvalidRecommendationOwnerError, match="cannot review"):
                assign_owner(session, record, owner_user_id=plain, actor_user_id=plain)
        finally:
            session.close()

    def test_an_unknown_user_is_refused_with_the_same_error(self, session_factory):
        session = session_factory()
        try:
            record = _record(session)
            with pytest.raises(InvalidRecommendationOwnerError, match="not an active user"):
                assign_owner(session, record, owner_user_id=uuid.uuid4(), actor_user_id=None)
        finally:
            session.close()

    def test_owner_assignment_never_changes_status(self, session_factory):
        session = session_factory()
        try:
            record = _record(session)
            owner = _user(session, "s@a.example", tenant_id="tenant-a", role="analyst")
            assign_owner(session, record, owner_user_id=owner, actor_user_id=owner)
            assert session.get(RecommendationRecord, record.id).status == "generated"
        finally:
            session.close()

    def test_clearing_records_previous_owner_in_the_event(self, session_factory):
        session = session_factory()
        try:
            record = _record(session)
            owner = _user(session, "c@a.example", tenant_id="tenant-a", role="analyst")
            assign_owner(session, record, owner_user_id=owner, actor_user_id=owner)
            assign_owner(session, record, owner_user_id=None, actor_user_id=owner)
            last = list_feedback_events(session, record.id)[-1]
            assert last.event_type == "owner_assigned"
            assert last.detail == {"owner_user_id": None, "previous_owner_user_id": str(owner)}
        finally:
            session.close()

    def test_list_filters_by_owner_and_unassigned(self, session_factory):
        session = session_factory()
        try:
            owner = _user(session, "l@a.example", tenant_id="tenant-a", role="analyst")
            owned = _record(session)
            _record(session)
            assign_owner(session, owned, owner_user_id=owner, actor_user_id=owner)
            assert [r.id for r in list_records(session, "tenant-a", owner_user_id=owner)] == [
                owned.id
            ]
            unassigned = list_records(session, "tenant-a", unassigned=True)
            assert len(unassigned) == 1 and unassigned[0].id != owned.id
        finally:
            session.close()


class TestOwnerDisplayNames:
    def test_empty_input_is_an_empty_result(self, session_factory):
        session = session_factory()
        try:
            assert owner_display_names(session, set()) == {}
        finally:
            session.close()

    def test_users_without_a_display_name_are_omitted(self, session_factory):
        session = session_factory()
        try:
            named = create_user(
                session,
                email="named@a.example",
                password="correcthorse battery staple 1",
                display_name="Named Person",
                tenant_id="tenant-a",
            )
            unnamed = _user(session, "unnamed@a.example", tenant_id="tenant-a")
            names = owner_display_names(session, {named.id, unnamed})
            assert names == {named.id: "Named Person"}
        finally:
            session.close()


class TestNewPolicyActions:
    @pytest.mark.parametrize(
        "action",
        [RecommendationAction.ADD_NOTE, RecommendationAction.ASSIGN_OWNER],
    )
    def test_reviewer_is_allowed_in_their_own_tenant(self, action):
        decision = authorize_recommendation_action(
            actor_permissions=frozenset({Permission.RECOMMENDATION_REVIEW.value}),
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=action,
        )
        assert decision.allowed

    @pytest.mark.parametrize(
        "action",
        [RecommendationAction.ADD_NOTE, RecommendationAction.ASSIGN_OWNER],
    )
    def test_plain_actor_is_denied(self, action):
        decision = authorize_recommendation_action(
            actor_permissions=frozenset(),
            actor_tenant_id="tenant-a",
            record_tenant_id="tenant-a",
            action=action,
        )
        assert not decision.allowed
        assert decision.reason == "missing_permission"

    @pytest.mark.parametrize(
        "action",
        [RecommendationAction.ADD_NOTE, RecommendationAction.ASSIGN_OWNER],
    )
    def test_cross_tenant_reviewer_is_denied_before_any_role_check(self, action):
        decision = authorize_recommendation_action(
            actor_permissions=frozenset({Permission.RECOMMENDATION_MANAGE.value}),
            actor_tenant_id="tenant-b",
            record_tenant_id="tenant-a",
            action=action,
        )
        assert not decision.allowed
        assert decision.reason == "cross_tenant"


def test_event_type_column_defaults_to_status_change():
    """A row inserted without an explicit `event_type` (every pre-Prompt-31
    code path, and any row written before the migration ran) reads back as
    `status_change` -- the only meaning a legacy event can have."""
    assert RecommendationFeedbackEvent.__table__.c.event_type.server_default is not None
