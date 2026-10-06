"""Persistence tests for semantic-intelligence findings and catalog rollback
(Prompt 35, `identity/repositories/semantic_intelligence.py`). Real ORM over an
in-memory SQLite database, the same pattern as the other identity repository
tests. No mocks of the persistence layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from identity.models import Base
from identity.repositories import semantic_intelligence as repo
from identity.repositories.semantic_catalog import (
    create_entry,
    entry_to_snapshot,
    list_entries,
)
from semantic.intelligence.detect import Finding
from semantic.intelligence.engine import run_analysis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 10, 6, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    s = maker()
    yield s
    s.close()
    engine.dispose()


def _finding(
    key: str = "k1", *, title: str = "Conflict", risk: int = 80, detail: str = "d"
) -> Finding:
    return Finding(
        kind="conflict",
        key=key,
        title=title,
        detail=detail,
        subjects=({"concept_key": "revenue"},),
        evidence=({"entry_id": "e1"},),
        confidence=0.9,
        risk_score=risk,
        risk_tier="high",
        reasons=("base 50",),
    )


def _upsert(session, *findings: Finding, tenant: str = "tenant-a"):
    return repo.upsert_findings(
        session, tenant_id=tenant, database_id="db1", findings=list(findings), now=NOW
    )


def _row(session, key: str = "k1", tenant: str = "tenant-a"):
    rows = repo.list_findings(session, tenant_id=tenant, database_id="db1")
    return next(r for r in rows if r.finding_key == key)


class TestUpsertVersioning:
    def test_a_new_finding_is_stored_open_at_version_one(self, session):
        summary = _upsert(session, _finding())

        assert (summary.created, summary.updated, summary.unchanged) == (1, 0, 0)
        row = _row(session)
        assert row.status == "open" and row.version == 1 and row.history == []
        assert row.truth_level == "ai_inference"

    def test_an_unchanged_rerun_keeps_the_same_version(self, session):
        _upsert(session, _finding())
        summary = _upsert(session, _finding())

        assert (summary.created, summary.unchanged) == (0, 1)
        assert _row(session).version == 1

    def test_a_changed_rerun_snapshots_the_old_content_and_bumps_the_version(self, session):
        _upsert(session, _finding(detail="old detail"))
        _upsert(session, _finding(detail="new detail"))

        row = _row(session)
        assert row.version == 2
        assert row.content["detail"] == "new detail"
        assert len(row.history) == 1
        assert row.history[0]["version"] == 1
        assert row.history[0]["content"]["detail"] == "old detail"

    def test_a_changed_finding_is_reopened_for_a_fresh_decision(self, session):
        _upsert(session, _finding(detail="a"))
        repo.decide_finding(
            session, _row(session), decision="accept", user_id=None, note="ok", now=NOW
        )
        _upsert(session, _finding(detail="b"))

        assert _row(session).status == "open"

    def test_history_is_append_only_across_several_changes(self, session):
        for detail in ("a", "b", "c"):
            _upsert(session, _finding(detail=detail))

        row = _row(session)
        assert [h["version"] for h in row.history] == [1, 2]
        assert row.version == 3


class TestDecisions:
    def test_accepting_a_finding_records_who_and_why_without_touching_truth(self, session):
        _upsert(session, _finding())
        row = repo.decide_finding(
            session,
            _row(session),
            decision="accept",
            user_id=None,
            note="confirmed by SME",
            now=NOW,
        )

        assert row.status == "accepted"
        assert row.decision_note == "confirmed by SME"
        assert row.truth_level == "ai_inference"

    def test_a_decided_finding_cannot_be_decided_again(self, session):
        _upsert(session, _finding())
        repo.decide_finding(
            session, _row(session), decision="dismiss", user_id=None, note=None, now=NOW
        )

        with pytest.raises(repo.InvalidFindingTransitionError):
            repo.decide_finding(
                session, _row(session), decision="accept", user_id=None, note=None, now=NOW
            )

    def test_an_unknown_decision_is_rejected(self, session):
        _upsert(session, _finding())
        with pytest.raises(repo.InvalidFindingTransitionError):
            repo.decide_finding(
                session, _row(session), decision="confirm", user_id=None, note=None, now=NOW
            )

    def test_a_dismissed_finding_stays_dismissed_on_an_unchanged_rerun(self, session):
        _upsert(session, _finding())
        repo.decide_finding(
            session, _row(session), decision="dismiss", user_id=None, note=None, now=NOW
        )
        _upsert(session, _finding())

        assert _row(session).status == "dismissed"


class TestRollback:
    def test_rollback_restores_an_earlier_version_as_a_new_version(self, session):
        _upsert(session, _finding(detail="original"))
        _upsert(session, _finding(detail="changed"))

        row = repo.rollback_finding(session, _row(session), to_version=1, user_id=None, now=NOW)

        assert row.version == 3
        assert row.content["detail"] == "original"
        assert row.status == "open"
        assert [h["version"] for h in row.history] == [1, 2]  # nothing removed from history

    def test_rolling_back_to_the_current_or_a_future_version_is_refused(self, session):
        _upsert(session, _finding(detail="a"))
        _upsert(session, _finding(detail="b"))

        with pytest.raises(repo.FindingVersionNotFoundError):
            repo.rollback_finding(session, _row(session), to_version=2, user_id=None, now=NOW)

    def test_rolling_back_to_a_version_outside_history_is_refused(self, session):
        _upsert(session, _finding(detail="a"))
        _upsert(session, _finding(detail="b"))

        with pytest.raises(repo.FindingVersionNotFoundError):
            repo.rollback_finding(session, _row(session), to_version=0, user_id=None, now=NOW)


class TestTenantIsolation:
    def test_another_tenants_finding_is_indistinguishable_from_a_missing_one(self, session):
        _upsert(session, _finding(), tenant="tenant-a")
        row = _row(session, tenant="tenant-a")

        assert repo.get_finding(session, tenant_id="tenant-b", finding_id=row.id) is None
        assert repo.get_finding(session, tenant_id="tenant-a", finding_id=row.id) is not None

    def test_the_queue_never_lists_another_tenants_findings(self, session):
        _upsert(session, _finding("k1"), tenant="tenant-a")
        _upsert(session, _finding("k2"), tenant="tenant-b")

        keys = {r.finding_key for r in repo.list_findings(session, tenant_id="tenant-a")}
        assert keys == {"k1"}

    def test_the_same_finding_key_in_two_tenants_is_two_independent_rows(self, session):
        _upsert(session, _finding("shared"), tenant="tenant-a")
        _upsert(session, _finding("shared", detail="other"), tenant="tenant-b")

        assert len(repo.list_findings(session, tenant_id="tenant-a")) == 1
        assert _row(session, "shared", tenant="tenant-b").version == 1


class TestCatalogRollback:
    def _entry(self, session, *, expression: str, version_note: str):
        return create_entry(
            session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            concept_key="revenue",
            business_name="Revenue",
            technical_name=None,
            description=version_note,
            grain=None,
            keys=[],
            relationships=[],
            domain=None,
            synonyms=[],
            business_rules=[],
            examples=[],
            evidence=[],
            confidence=1.0,
            owner="finance",
            created_by_user_id=None,
            approved_expression=expression,
            source_tables=["sales"],
            filters=[],
            dimensions=[],
            aggregation="sum",
        )

    def test_rollback_creates_a_new_draft_with_the_older_content(self, session):
        v1 = self._entry(session, expression="SUM(a)", version_note="first")
        v2 = self._entry(session, expression="SUM(b)", version_note="second")

        new = repo.rollback_catalog_entry(
            session, v2, target_version=v1.version, actor_user_id=None
        )

        assert new.version == 3 and new.status == "draft"
        assert new.approved_expression == "SUM(a)"
        assert new.description == "first"

    def test_rollback_never_modifies_an_existing_version(self, session):
        v1 = self._entry(session, expression="SUM(a)", version_note="first")
        v2 = self._entry(session, expression="SUM(b)", version_note="second")
        before = [
            (e.version, e.approved_expression, e.status) for e in list_entries(session, "tenant-a")
        ]

        repo.rollback_catalog_entry(session, v2, target_version=v1.version, actor_user_id=None)

        after = [
            (e.version, e.approved_expression, e.status) for e in list_entries(session, "tenant-a")
        ]
        assert set(before) <= set(after)

    def test_rolling_back_to_a_nonexistent_version_is_refused(self, session):
        v1 = self._entry(session, expression="SUM(a)", version_note="first")

        with pytest.raises(repo.CatalogVersionNotFoundError):
            repo.rollback_catalog_entry(session, v1, target_version=99, actor_user_id=None)


class TestEngineToPersistence:
    def test_a_conflict_between_stored_definitions_is_persisted_as_ai_inference(self, session):
        self_entries = [
            self._entry_row(session, "revenue_gross", "SUM(gross)"),
            self._entry_row(session, "revenue_net", "SUM(net)"),
        ]
        snapshots = [entry_to_snapshot(e) for e in self_entries]
        result = run_analysis(snapshots)
        repo.upsert_findings(
            session,
            tenant_id="tenant-a",
            database_id="db1",
            findings=list(result.findings),
            now=NOW,
        )

        conflicts = [
            r
            for r in repo.list_findings(session, tenant_id="tenant-a", database_id="db1")
            if r.kind == "conflict"
        ]
        assert conflicts and all(r.truth_level == "ai_inference" for r in conflicts)

    def _entry_row(self, session, key: str, expression: str):
        return create_entry(
            session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            concept_key=key,
            business_name="Revenue",
            technical_name=None,
            description="",
            grain=None,
            keys=[],
            relationships=[],
            domain=None,
            synonyms=[],
            business_rules=[],
            examples=[],
            evidence=[],
            confidence=1.0,
            owner=None,
            created_by_user_id=None,
            approved_expression=expression,
            source_tables=["sales"],
            filters=[],
            dimensions=[],
            aggregation="sum",
        )
