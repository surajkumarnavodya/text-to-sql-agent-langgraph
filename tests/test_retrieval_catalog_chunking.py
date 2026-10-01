"""Unit tests for retrieval/chunking.py's
`business_concept_chunk_from_catalog_entry` (Prompt 09,
`09_SEMANTIC_CATALOG_CONTRACT.md`). Fully offline: a plain
`CatalogEntrySnapshot` in, a plain `Chunk` out -- no vector store, no
embedding model.
"""

from __future__ import annotations

from typing import Any

from retrieval.chunking import business_concept_chunk_from_catalog_entry
from retrieval.models import ChunkType
from semantic.catalog import CatalogConceptType, CatalogEntrySnapshot, CatalogStatus


def _snapshot(**overrides: Any) -> CatalogEntrySnapshot:
    defaults: dict[str, Any] = dict(
        id="entry-1",
        tenant_id="tenant-a",
        database_id="db1",
        concept_type=CatalogConceptType.METRIC,
        concept_key="clv",
        business_name="Customer Lifetime Value",
        technical_name="SUM(Amount)",
        description="Total historical revenue per customer",
        grain="one row per customer",
        keys=("CustomerId",),
        relationships=(),
        domain="Sales",
        synonyms=("CLV",),
        business_rules=("Exclude refunded orders",),
        examples=("What is the average CLV by region?",),
        evidence=({"signal": "manual", "score": 1.0, "detail": "authored by analyst"},),
        confidence=0.95,
        status=CatalogStatus.PUBLISHED,
        owner="alice",
        version=1,
    )
    defaults.update(overrides)
    return CatalogEntrySnapshot(**defaults)


class TestBusinessConceptChunkShape:
    def test_chunk_type_is_business_concept(self):
        chunk = business_concept_chunk_from_catalog_entry(_snapshot(), "minilm", 384)
        assert chunk.chunk_type == ChunkType.BUSINESS_CONCEPT

    def test_database_id_and_table_name_come_from_the_snapshot(self):
        chunk = business_concept_chunk_from_catalog_entry(_snapshot(), "minilm", 384)
        assert chunk.database_id == "db1"
        assert chunk.table_name == "SUM(Amount)"

    def test_text_is_self_contained_and_includes_every_populated_field(self):
        chunk = business_concept_chunk_from_catalog_entry(_snapshot(), "minilm", 384)
        assert "Customer Lifetime Value" in chunk.text
        assert "SUM(Amount)" in chunk.text
        assert "one row per customer" in chunk.text
        assert "CustomerId" in chunk.text
        assert "Sales" in chunk.text
        assert "CLV" in chunk.text
        assert "Exclude refunded orders" in chunk.text
        assert "average CLV by region" in chunk.text

    def test_extra_carries_the_full_governance_fields(self):
        chunk = business_concept_chunk_from_catalog_entry(_snapshot(), "minilm", 384)
        assert chunk.extra["concept_type"] == "metric"
        assert chunk.extra["concept_key"] == "clv"
        assert chunk.extra["status"] == "published"
        assert chunk.extra["confidence"] == 0.95
        assert chunk.extra["owner"] == "alice"
        assert chunk.extra["version"] == 1
        assert chunk.extra["truth_level"] == "confirmed_business_truth"
        assert chunk.extra["evidence"] == [
            {"signal": "manual", "score": 1.0, "detail": "authored by analyst"}
        ]

    def test_tags_are_the_synonyms(self):
        chunk = business_concept_chunk_from_catalog_entry(_snapshot(), "minilm", 384)
        assert chunk.tags == ("CLV",)

    def test_a_concept_with_no_optional_fields_still_produces_a_valid_chunk(self):
        snapshot = _snapshot(
            technical_name=None,
            description="",
            grain=None,
            keys=(),
            relationships=(),
            domain=None,
            synonyms=(),
            business_rules=(),
            examples=(),
        )
        chunk = business_concept_chunk_from_catalog_entry(snapshot, "minilm", 384)
        assert chunk.text.strip() == "Metric: Customer Lifetime Value."


class TestChunkIdUniquenessPerVersion:
    def test_a_new_version_gets_a_different_chunk_id(self):
        v1 = business_concept_chunk_from_catalog_entry(_snapshot(version=1), "minilm", 384)
        v2 = business_concept_chunk_from_catalog_entry(_snapshot(version=2), "minilm", 384)
        assert v1.chunk_id != v2.chunk_id

    def test_the_same_version_produces_the_same_chunk_id_deterministically(self):
        a = business_concept_chunk_from_catalog_entry(_snapshot(version=1), "minilm", 384)
        b = business_concept_chunk_from_catalog_entry(_snapshot(version=1), "minilm", 384)
        assert a.chunk_id == b.chunk_id

    def test_different_concept_types_with_the_same_key_never_collide(self):
        metric = business_concept_chunk_from_catalog_entry(
            _snapshot(concept_type=CatalogConceptType.METRIC, concept_key="customer"),
            "minilm",
            384,
        )
        entity = business_concept_chunk_from_catalog_entry(
            _snapshot(concept_type=CatalogConceptType.ENTITY, concept_key="customer"),
            "minilm",
            384,
        )
        assert metric.chunk_id != entity.chunk_id

    def test_a_relationship_entry_renders_related_concepts(self):
        snapshot = _snapshot(
            concept_type=CatalogConceptType.ENTITY,
            concept_key="customer",
            business_name="Customer",
            relationships=({"related_concept_key": "orders", "relationship_type": "has_many"},),
        )
        chunk = business_concept_chunk_from_catalog_entry(snapshot, "minilm", 384)
        assert "orders" in chunk.text
        assert "has_many" in chunk.text
