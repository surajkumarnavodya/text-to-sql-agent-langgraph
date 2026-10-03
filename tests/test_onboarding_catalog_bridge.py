"""Unit tests for `onboarding/catalog_bridge.py` -- Prompt 32.

Real ORM rows on an in-memory SQLite identity database (the same pattern as
`tests/test_identity_repositories_onboarding.py`), no mocks of persistence. What
is checked:

- Only **confirmed** semantic-label and relationship items are bridged. Pending
  and rejected items leave no trace, the same rule the semantic contract applies.
- Every bridged entry is a **draft** entity. Nothing is published by the bridge.
- A second run creates nothing (idempotent on a retried publish).
"""

from __future__ import annotations

import pytest
from identity.bootstrap import seed_rbac
from identity.models import Base, OnboardingReviewItem, SemanticCatalogEntry
from identity.repositories.onboarding import add_review_items, create_job
from identity.repositories.semantic_catalog import list_entries
from identity.repositories.tenants import create_tenant
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from onboarding.catalog_bridge import catalog_database_id_for_job, draft_catalog_entries_for_job


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=True)
    s = factory()
    seed_rbac(s)
    create_tenant(s, tenant_id="tenant-a", name="A")
    s.commit()
    yield s
    s.close()
    engine.dispose()


def _job(session):
    return create_job(
        session,
        tenant_id="tenant-a",
        created_by_user_id=None,
        database_label="My Warehouse",
        db_type="postgresql",
        db_host="db.internal",
        db_port=5432,
        db_name="warehouse",
        db_user="svc",
        db_schema=None,
    )


def _decide(session, items: list[OnboardingReviewItem], decision: str) -> None:
    for item in items:
        item.decision = decision
    session.commit()


def _seed_items(session, job):
    """Items covering each decision state and both bridged item kinds."""
    rows = add_review_items(
        session,
        job,
        [
            {
                "item_type": "semantic_label",
                "table_name": "Customers",
                "column_name": "Email",
                "subject": "Customers.Email",
                "payload": {"label": "email"},
                "confidence": 0.9,
                "is_ambiguous": False,
            },
            {
                "item_type": "semantic_label",
                "table_name": "Orders",
                "column_name": "Amount",
                "subject": "Orders.Amount",
                "payload": {"label": "amount"},
                "confidence": 0.8,
                "is_ambiguous": False,
            },
            {
                "item_type": "relationship",
                "table_name": "Orders",
                "column_name": None,
                "subject": "Orders -> Customers",
                "payload": {
                    "source_table": "Orders",
                    "target_table": "Customers",
                    "source_columns": ["CustomerId"],
                    "target_columns": ["Id"],
                },
                "confidence": 0.7,
                "is_ambiguous": False,
            },
            {
                "item_type": "pii_classification",
                "table_name": "Refunds",
                "column_name": "CardNumber",
                "subject": "Refunds.CardNumber",
                "payload": {"tier": "restricted"},
                "confidence": 0.95,
                "is_ambiguous": False,
            },
            {
                "item_type": "semantic_label",
                "table_name": "Shipments",
                "column_name": "Carrier",
                "subject": "Shipments.Carrier",
                "payload": {"label": "carrier"},
                "confidence": 0.6,
                "is_ambiguous": False,
            },
        ],
    )
    return rows


def _by_subject(rows, subject: str) -> OnboardingReviewItem:
    return next(row for row in rows if row.subject == subject)


class TestOnlyConfirmedItemsAreBridged:
    def test_pending_and_rejected_items_leave_no_catalog_trace(self, session):
        job = _job(session)
        rows = _seed_items(session, job)
        _by_subject(rows, "Customers.Email").decision = "confirmed"
        _by_subject(rows, "Orders.Amount").decision = "confirmed"
        _by_subject(rows, "Orders -> Customers").decision = "confirmed"
        _by_subject(rows, "Shipments.Carrier").decision = "rejected"
        session.commit()
        # `Refunds.CardNumber` is a pending PII classification: never bridged.

        draft_catalog_entries_for_job(session, job, list(rows))

        keys = {e.concept_key for e in list_entries(session, "tenant-a", concept_type="entity")}
        assert keys == {"table:Customers", "table:Orders"}
        assert "table:Refunds" not in keys
        assert "table:Shipments" not in keys

    def test_a_table_with_only_pending_items_is_not_created(self, session):
        job = _job(session)
        rows = _seed_items(session, job)
        _decide(session, rows, "pending")
        created = draft_catalog_entries_for_job(session, job, list(rows))
        assert created == 0
        assert list_entries(session, "tenant-a", concept_type="entity") == []


class TestBridgedEntriesAreDrafts:
    def test_every_bridged_entry_is_a_draft_entity_for_this_database(self, session):
        job = _job(session)
        rows = _seed_items(session, job)
        _decide(session, rows, "confirmed")

        draft_catalog_entries_for_job(session, job, list(rows))

        database_id = catalog_database_id_for_job(job)
        entries = list_entries(session, "tenant-a", database_id=database_id)
        assert entries, "expected draft entries for the confirmed tables"
        for entry in entries:
            assert entry.status == "draft"
            assert entry.concept_type == "entity"
            assert entry.database_id == database_id
            assert entry.version == 1

    def test_the_database_id_is_a_valid_vector_store_name(self, session):
        """The catalog's database_id names a Chroma collection, which only accepts
        `[A-Za-z0-9_-]`. A label with spaces and punctuation must still produce a
        valid, non-empty id, and two labels must not collide."""
        import re

        job = _job(session)
        other = create_job(
            session,
            tenant_id="tenant-a",
            created_by_user_id=None,
            database_label="My-Warehouse!!",
            db_type="postgresql",
            db_host=None,
            db_port=None,
            db_name=None,
            db_user=None,
            db_schema=None,
        )
        safe = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*[A-Za-z0-9]$")
        for candidate in (job, other):
            assert safe.match(catalog_database_id_for_job(candidate))
        assert catalog_database_id_for_job(job) != catalog_database_id_for_job(other)

    def test_a_label_with_no_usable_characters_still_gets_an_id(self, session):
        job = create_job(
            session,
            tenant_id="tenant-a",
            created_by_user_id=None,
            database_label="!!!",
            db_type="postgresql",
            db_host=None,
            db_port=None,
            db_name=None,
            db_user=None,
            db_schema=None,
        )
        assert catalog_database_id_for_job(job).startswith("onboarded_")

    def test_evidence_names_the_job_and_the_truth_level(self, session):
        job = _job(session)
        rows = _seed_items(session, job)
        _decide(session, rows, "confirmed")
        draft_catalog_entries_for_job(session, job, list(rows))

        customers = next(
            e for e in list_entries(session, "tenant-a") if e.concept_key == "table:Customers"
        )
        assert customers.evidence[0]["source"] == "onboarding_job"
        assert customers.evidence[0]["job_id"] == str(job.id)
        assert customers.evidence[0]["truth_level"] == "ai_inference"
        assert "SME-confirmed" in customers.description
        assert 0.0 <= customers.confidence <= 1.0

    def test_confidence_is_the_mean_of_the_tables_confirmed_items(self, session):
        job = _job(session)
        rows = _seed_items(session, job)
        _decide(session, rows, "confirmed")
        draft_catalog_entries_for_job(session, job, list(rows))

        orders = next(
            e for e in list_entries(session, "tenant-a") if e.concept_key == "table:Orders"
        )
        # Orders has an Amount label (0.8) and a relationship (0.7).
        assert orders.confidence == pytest.approx(0.75)


class TestIdempotency:
    def test_a_second_run_creates_nothing(self, session):
        job = _job(session)
        rows = _seed_items(session, job)
        _decide(session, rows, "confirmed")

        first = draft_catalog_entries_for_job(session, job, list(rows))
        second = draft_catalog_entries_for_job(session, job, list(rows))

        assert first == 3
        assert second == 0
        assert len(list_entries(session, "tenant-a", concept_type="entity")) == 3

    def test_a_superseded_entry_does_not_block_a_fresh_draft(self, session):
        """Only a *live* (non-superseded) entity blocks creation. A concept that
        was superseded earlier is free to get a new draft."""
        job = _job(session)
        rows = _seed_items(session, job)
        _decide(session, rows, "confirmed")
        draft_catalog_entries_for_job(session, job, list(rows))

        customers = next(
            e for e in list_entries(session, "tenant-a") if e.concept_key == "table:Customers"
        )
        customers.status = "superseded"
        session.commit()

        created = draft_catalog_entries_for_job(session, job, list(rows))
        assert created == 1
        assert (
            len(session.query(SemanticCatalogEntry).filter_by(concept_key="table:Customers").all())
            == 2
        )
