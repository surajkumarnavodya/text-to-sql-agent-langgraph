"""Unit tests for retrieval/retriever.py::extract_governing_metrics
(Prompt 10, `10_GOVERNED_METRICS_CONTRACT.md`). Fully offline: plain
`Chunk`/`ScoredChunk` values in, plain dicts out -- no vector store, no
embedding model.
"""

from __future__ import annotations

from typing import Any

from retrieval.chunking import business_concept_chunk_from_catalog_entry
from retrieval.models import ScoredChunk
from retrieval.retriever import extract_governing_metrics
from semantic.catalog import CatalogConceptType, CatalogEntrySnapshot, CatalogStatus


def _snapshot(**overrides: Any) -> CatalogEntrySnapshot:
    defaults: dict[str, Any] = dict(
        id="x",
        tenant_id="t1",
        database_id="db1",
        concept_type=CatalogConceptType.METRIC,
        concept_key="aov",
        business_name="Average Order Value",
        approved_expression="SUM(SalesAmount) / COUNT(DISTINCT SalesOrderNumber)",
        aggregation="derived ratio",
        status=CatalogStatus.PUBLISHED,
        version=1,
    )
    defaults.update(overrides)
    return CatalogEntrySnapshot(**defaults)


def _scored_chunk(snapshot, similarity=0.9):
    chunk = business_concept_chunk_from_catalog_entry(snapshot, "minilm", 384)
    return ScoredChunk(chunk=chunk, vector_similarity=similarity)


class TestExtractGoverningMetrics:
    def test_a_metric_type_business_concept_chunk_is_extracted(self):
        items = [_scored_chunk(_snapshot())]
        governing = extract_governing_metrics(items)
        assert len(governing) == 1
        assert governing[0]["business_name"] == "Average Order Value"
        assert governing[0]["approved_expression"] == (
            "SUM(SalesAmount) / COUNT(DISTINCT SalesOrderNumber)"
        )
        assert governing[0]["aggregation"] == "derived ratio"
        assert "Average Order Value" in governing[0]["text"]

    def test_a_non_metric_business_concept_chunk_is_excluded(self):
        entity_snapshot = _snapshot(
            concept_type=CatalogConceptType.ENTITY,
            concept_key="customer",
            business_name="Customer",
            approved_expression=None,
            aggregation=None,
        )
        items = [_scored_chunk(entity_snapshot)]
        assert extract_governing_metrics(items) == []

    def test_a_non_business_concept_chunk_type_is_excluded(self):
        """A table/column/relationship/glossary/metric(YAML)/sql_example/
        documentation chunk -- anything that isn't retrieval.models
        .ChunkType.BUSINESS_CONCEPT at all -- is never mistaken for a
        governing metric."""
        from retrieval.models import Chunk, ChunkType

        other_chunk = Chunk(
            chunk_id="abc",
            chunk_type=ChunkType.METRIC,
            text="Metric: gross sales amount. Formula: SUM(SalesAmount)",
            database_id="db1",
            source_id="metric:gross_sales_amount",
            content_hash="deadbeef",
            embedding_model="minilm",
            embedding_dimensions=384,
            extra={"formula": "SUM(SalesAmount)"},
        )
        items = [ScoredChunk(chunk=other_chunk, vector_similarity=0.9)]
        assert extract_governing_metrics(items) == []

    def test_mixed_results_only_metric_entries_survive(self):
        metric = _scored_chunk(_snapshot())
        entity = _scored_chunk(
            _snapshot(
                concept_type=CatalogConceptType.ENTITY,
                concept_key="customer",
                business_name="Customer",
                approved_expression=None,
                aggregation=None,
            )
        )
        governing = extract_governing_metrics([entity, metric])
        assert len(governing) == 1
        assert governing[0]["business_name"] == "Average Order Value"

    def test_empty_input_produces_empty_output(self):
        assert extract_governing_metrics([]) == []

    def test_two_different_phrasings_retrieving_the_same_metric_chunk_produce_identical_dicts(
        self,
    ):
        """The direct mechanism behind the acceptance criterion
        ('equivalent KPI questions consistently use the governed metric
        definition'): the extracted dict is a deterministic function of
        the chunk, never of whatever question text the similarity search
        happened to be run with."""
        snapshot = _snapshot()
        phrasing_one = [_scored_chunk(snapshot, similarity=0.81)]
        phrasing_two = [_scored_chunk(snapshot, similarity=0.77)]

        governing_one = extract_governing_metrics(phrasing_one)
        governing_two = extract_governing_metrics(phrasing_two)

        assert governing_one == governing_two

    def test_a_metric_with_no_approved_expression_still_has_a_business_name_and_text(self):
        snapshot = _snapshot(approved_expression=None, aggregation=None)
        items = [_scored_chunk(snapshot)]
        governing = extract_governing_metrics(items)
        assert governing[0]["approved_expression"] is None
        assert governing[0]["business_name"] == "Average Order Value"
        assert governing[0]["text"]
