"""Unit tests for identity/repositories/semantic_catalog.py (Prompt 09,
`09_SEMANTIC_CATALOG_CONTRACT.md`) -- against a real in-memory SQLite
database, same convention as
tests/test_identity_repositories_onboarding.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from identity.models import Base
from identity.repositories.semantic_catalog import (
    InvalidCatalogStatusTransitionError,
    create_entry,
    entry_to_snapshot,
    find_conflicting_published_entries,
    get_entry_by_id,
    list_entries,
    list_versions_for_concept_key,
    mark_reviewed,
    next_version_for_concept_key,
    publish_entry,
    request_changes,
    update_draft_entry,
)
from semantic.catalog import CatalogStatus
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

_REVIEWER = uuid.uuid4()
_PUBLISHER = uuid.uuid4()


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


def _create(session: Session, *, tenant_id="tenant-a", concept_key="clv", **overrides):
    defaults = dict(
        tenant_id=tenant_id,
        database_id="db1",
        concept_type="metric",
        concept_key=concept_key,
        business_name="Customer Lifetime Value",
        technical_name="SUM(Amount)",
        description="Total historical revenue per customer",
        grain="one row per customer",
        keys=["CustomerId"],
        relationships=[],
        domain="Sales",
        synonyms=["CLV"],
        business_rules=[],
        examples=[],
        evidence=[],
        confidence=0.9,
        owner="alice",
        created_by_user_id=None,
    )
    defaults.update(overrides)
    return create_entry(session, **defaults)


class TestCreateEntry:
    def test_creates_a_draft_at_version_one(self, db_session: Session):
        entry = _create(db_session)
        assert entry.status == "draft"
        assert entry.version == 1

    def test_next_version_for_a_brand_new_concept_key_is_one(self, db_session: Session):
        version = next_version_for_concept_key(
            db_session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            concept_key="new",
        )
        assert version == 1

    def test_creating_again_for_the_same_concept_key_increments_version(self, db_session: Session):
        first = _create(db_session)
        second = _create(db_session, business_name="Customer Lifetime Value v2")
        assert first.version == 1
        assert second.version == 2
        assert first.id != second.id


class TestGetAndListEntries:
    def test_get_entry_by_id_returns_none_for_a_missing_id(self, db_session: Session):
        assert get_entry_by_id(db_session, uuid.uuid4()) is None

    def test_get_entry_by_id_returns_the_created_entry(self, db_session: Session):
        entry = _create(db_session)
        fetched = get_entry_by_id(db_session, entry.id)
        assert fetched is not None
        assert fetched.id == entry.id

    def test_list_entries_only_returns_that_tenants_entries(self, db_session: Session):
        a = _create(db_session, tenant_id="tenant-a")
        _create(db_session, tenant_id="tenant-b")
        entries = list_entries(db_session, "tenant-a")
        assert [e.id for e in entries] == [a.id]

    def test_list_entries_filters_by_database_concept_type_and_status(self, db_session: Session):
        _create(db_session, concept_key="clv", concept_type="metric")
        _create(db_session, concept_key="customer", concept_type="entity")

        metrics = list_entries(db_session, "tenant-a", concept_type="metric")
        assert [e.concept_key for e in metrics] == ["clv"]

        drafts = list_entries(db_session, "tenant-a", status="draft")
        assert len(drafts) == 2

        none_published = list_entries(db_session, "tenant-a", status="published")
        assert none_published == []

        wrong_db = list_entries(db_session, "tenant-a", database_id="other-db")
        assert wrong_db == []


class TestUpdateDraftEntry:
    def test_updates_fields_in_place_while_draft(self, db_session: Session):
        entry = _create(db_session)
        updated = update_draft_entry(db_session, entry, business_name="New Name", confidence=0.5)
        assert updated.business_name == "New Name"
        assert updated.confidence == 0.5

    def test_cannot_edit_a_non_draft_entry(self, db_session: Session):
        entry = _create(db_session)
        mark_reviewed(db_session, entry, reviewed_by_user_id=_REVIEWER)
        with pytest.raises(InvalidCatalogStatusTransitionError):
            update_draft_entry(db_session, entry, business_name="Nope")


class TestReviewTransitions:
    def test_mark_reviewed_moves_draft_to_reviewed(self, db_session: Session):
        entry = _create(db_session)
        reviewer = uuid.uuid4()
        result = mark_reviewed(db_session, entry, reviewed_by_user_id=reviewer, notes="looks good")
        assert result.status == "reviewed"
        assert result.reviewed_by_user_id == reviewer
        assert result.reviewed_at is not None
        assert result.review_notes == "looks good"

    def test_mark_reviewed_on_a_non_draft_entry_raises(self, db_session: Session):
        entry = _create(db_session)
        mark_reviewed(db_session, entry, reviewed_by_user_id=_REVIEWER)
        with pytest.raises(InvalidCatalogStatusTransitionError):
            mark_reviewed(db_session, entry, reviewed_by_user_id=_REVIEWER)

    def test_request_changes_moves_reviewed_back_to_draft(self, db_session: Session):
        entry = _create(db_session)
        mark_reviewed(db_session, entry, reviewed_by_user_id=_REVIEWER)
        result = request_changes(db_session, entry, reviewed_by_user_id=_REVIEWER, notes="fix this")
        assert result.status == "draft"
        assert result.review_notes == "fix this"

    def test_request_changes_on_a_draft_entry_raises(self, db_session: Session):
        entry = _create(db_session)
        with pytest.raises(InvalidCatalogStatusTransitionError):
            request_changes(db_session, entry, reviewed_by_user_id=_REVIEWER)


class TestPublishAndSupersede:
    def test_publish_moves_reviewed_to_published(self, db_session: Session):
        entry = _create(db_session)
        mark_reviewed(db_session, entry, reviewed_by_user_id=_REVIEWER)
        publisher = uuid.uuid4()
        published, superseded = publish_entry(db_session, entry, published_by_user_id=publisher)
        assert published.status == "published"
        assert published.published_by_user_id == publisher
        assert published.published_at is not None
        assert superseded is None

    def test_publishing_a_draft_entry_raises(self, db_session: Session):
        entry = _create(db_session)
        with pytest.raises(InvalidCatalogStatusTransitionError):
            publish_entry(db_session, entry, published_by_user_id=_PUBLISHER)

    def test_publishing_a_new_version_supersedes_the_prior_published_one(self, db_session: Session):
        v1 = _create(db_session)
        mark_reviewed(db_session, v1, reviewed_by_user_id=_REVIEWER)
        publish_entry(db_session, v1, published_by_user_id=_PUBLISHER)

        v2 = _create(db_session, business_name="Customer Lifetime Value v2")
        mark_reviewed(db_session, v2, reviewed_by_user_id=_REVIEWER)
        published_v2, superseded_v1 = publish_entry(db_session, v2, published_by_user_id=_PUBLISHER)

        assert published_v2.status == "published"
        assert superseded_v1 is not None
        assert superseded_v1.id == v1.id
        assert superseded_v1.status == "superseded"
        assert published_v2.supersedes_id == v1.id

    def test_publishing_an_already_superseded_entry_raises(self, db_session: Session):
        v1 = _create(db_session)
        mark_reviewed(db_session, v1, reviewed_by_user_id=_REVIEWER)
        publish_entry(db_session, v1, published_by_user_id=_PUBLISHER)

        v2 = _create(db_session, business_name="v2")
        mark_reviewed(db_session, v2, reviewed_by_user_id=_REVIEWER)
        publish_entry(db_session, v2, published_by_user_id=_PUBLISHER)

        with pytest.raises(InvalidCatalogStatusTransitionError):
            publish_entry(db_session, v1, published_by_user_id=_PUBLISHER)

    def test_list_versions_returns_newest_first(self, db_session: Session):
        v1 = _create(db_session)
        mark_reviewed(db_session, v1, reviewed_by_user_id=_REVIEWER)
        publish_entry(db_session, v1, published_by_user_id=_PUBLISHER)
        # A new draft version exists alongside the still-live published
        # v1 until it is itself published (see the separate supersession
        # test above for that transition).
        _create(db_session, business_name="v2")

        versions = list_versions_for_concept_key(
            db_session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            concept_key="clv",
        )
        assert [v.version for v in versions] == [2, 1]
        assert [v.status for v in versions] == ["draft", "published"]


class TestEntryToSnapshot:
    def test_snapshot_round_trips_every_field(self, db_session: Session):
        entry = _create(
            db_session,
            approved_expression="SUM(Amount)",
            source_tables=["FactSales"],
            filters=["Region"],
            dimensions=["Region", "ProductCategory"],
            aggregation="SUM",
        )
        snapshot = entry_to_snapshot(entry)
        assert snapshot.tenant_id == "tenant-a"
        assert snapshot.database_id == "db1"
        assert snapshot.concept_type.value == "metric"
        assert snapshot.concept_key == "clv"
        assert snapshot.business_name == "Customer Lifetime Value"
        assert snapshot.keys == ("CustomerId",)
        assert snapshot.synonyms == ("CLV",)
        assert snapshot.confidence == 0.9
        assert snapshot.status == CatalogStatus.DRAFT
        assert snapshot.owner == "alice"
        assert snapshot.version == 1
        assert snapshot.approved_expression == "SUM(Amount)"
        assert snapshot.source_tables == ("FactSales",)
        assert snapshot.filters == ("Region",)
        assert snapshot.dimensions == ("Region", "ProductCategory")
        assert snapshot.aggregation == "SUM"

    def test_governed_metric_fields_default_to_empty(self, db_session: Session):
        entry = _create(db_session)
        snapshot = entry_to_snapshot(entry)
        assert snapshot.approved_expression is None
        assert snapshot.source_tables == ()
        assert snapshot.filters == ()
        assert snapshot.dimensions == ()
        assert snapshot.aggregation is None


class TestFindConflictingPublishedEntries:
    def _publish(self, session: Session, entry) -> None:
        mark_reviewed(session, entry, reviewed_by_user_id=_REVIEWER)
        publish_entry(session, entry, published_by_user_id=_PUBLISHER)

    def test_no_conflict_when_no_other_published_entry_shares_a_name_or_synonym(
        self, db_session: Session
    ):
        conflicts = find_conflicting_published_entries(
            db_session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            business_name="Average Order Value",
            synonyms=["AOV"],
            exclude_concept_key="aov",
        )
        assert conflicts == []

    def test_detects_a_different_concept_key_sharing_a_synonym(self, db_session: Session):
        existing = _create(
            db_session, concept_key="clv", business_name="Customer Lifetime Value", synonyms=["CLV"]
        )
        self._publish(db_session, existing)

        conflicts = find_conflicting_published_entries(
            db_session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            business_name="Cumulative Lifetime Value (alt definition)",
            synonyms=["CLV"],
            exclude_concept_key="clv-alt",
        )
        assert [c.concept_key for c in conflicts] == ["clv"]

    def test_detects_a_different_concept_key_sharing_a_business_name_case_insensitively(
        self, db_session: Session
    ):
        existing = _create(db_session, concept_key="revenue", business_name="Revenue", synonyms=[])
        self._publish(db_session, existing)

        conflicts = find_conflicting_published_entries(
            db_session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            business_name="REVENUE",
            synonyms=[],
            exclude_concept_key="revenue-v2",
        )
        assert [c.concept_key for c in conflicts] == ["revenue"]

    def test_the_same_concept_key_is_never_reported_as_its_own_conflict(self, db_session: Session):
        existing = _create(db_session, concept_key="clv")
        self._publish(db_session, existing)

        conflicts = find_conflicting_published_entries(
            db_session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            business_name=existing.business_name,
            synonyms=list(existing.synonyms),
            exclude_concept_key="clv",
        )
        assert conflicts == []

    def test_a_draft_or_reviewed_entry_never_counts_as_a_conflict(self, db_session: Session):
        draft = _create(
            db_session, concept_key="clv-draft", business_name="Customer Lifetime Value"
        )
        reviewed = _create(
            db_session, concept_key="clv-reviewed", business_name="Customer Lifetime Value"
        )
        mark_reviewed(db_session, reviewed, reviewed_by_user_id=_REVIEWER)

        conflicts = find_conflicting_published_entries(
            db_session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            business_name="Customer Lifetime Value",
            synonyms=[],
            exclude_concept_key="clv-new",
        )
        assert conflicts == []
        assert draft.status == "draft"
        assert reviewed.status == "reviewed"

    def test_scoped_to_tenant_database_and_concept_type(self, db_session: Session):
        same_name_other_tenant = _create(
            db_session,
            tenant_id="tenant-b",
            concept_key="clv",
            business_name="Customer Lifetime Value",
        )
        self._publish(db_session, same_name_other_tenant)

        conflicts = find_conflicting_published_entries(
            db_session,
            tenant_id="tenant-a",
            database_id="db1",
            concept_type="metric",
            business_name="Customer Lifetime Value",
            synonyms=[],
            exclude_concept_key="clv-new",
        )
        assert conflicts == []
