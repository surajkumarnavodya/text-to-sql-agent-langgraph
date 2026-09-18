"""Unit tests for retrieval/retriever.py + retrieval/reranker.py --
deduplication, diversity, context-size budget, role-based metadata
filtering, and the reranker's scoring formula.

Fully offline (FakeEmbeddingProvider + InMemoryVectorStore, see both
modules' docstrings for why).
"""

from __future__ import annotations

from pathlib import Path

from retrieval.embeddings import FakeEmbeddingProvider
from retrieval.ingestion import build_knowledge_chunks
from retrieval.models import Chunk, ChunkType, ScoredChunk, make_chunk_id
from retrieval.reranker import rerank
from retrieval.retriever import retrieve_business_context

from config.settings import Settings
from tests._retrieval_fakes import InMemoryVectorStore


def _settings(tmp_path: Path, **overrides) -> Settings:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(exist_ok=True)
    base = {
        "retrieval_embedding_provider": "fake",
        "retrieval_knowledge_dir": knowledge_dir,
        "retrieval_top_k_tables": 3,
        "retrieval_top_k_columns": 3,
        "retrieval_top_k_relationships": 3,
        "retrieval_top_k_glossary": 3,
        "retrieval_top_k_metrics": 3,
        "retrieval_top_k_sql_examples": 3,
        "retrieval_top_k_documentation": 3,
        "retrieval_similarity_threshold": 0.0,
    }
    base.update(overrides)
    return Settings(**base)


def _chunk(
    chunk_type: ChunkType,
    object_name: str,
    text: str,
    table_name: str | None = None,
    allowed_roles: tuple[str, ...] = (),
    content_hash: str | None = None,
) -> Chunk:
    return Chunk(
        chunk_id=make_chunk_id("default", None, object_name, chunk_type),
        chunk_type=chunk_type,
        text=text,
        database_id="default",
        table_name=table_name,
        source_id=f"{chunk_type.value}:{object_name}",
        content_hash=content_hash or f"hash-{object_name}",
        allowed_roles=allowed_roles,
        embedding_model="fake",
        embedding_dimensions=8,
    )


class TestRetrievalDeduplication:
    def test_two_chunks_sharing_a_content_hash_only_one_survives(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider(dimensions=16)

        shared_text = "Duplicate content appearing in two different chunks."
        chunk_a = _chunk(ChunkType.TABLE, "TableA", shared_text, content_hash="same-hash")
        chunk_b = _chunk(ChunkType.DOCUMENTATION, "doc1", shared_text, content_hash="same-hash")
        vectors = provider.embed_batch([chunk_a.text, chunk_b.text])
        store.upsert_documents("default", [chunk_a, chunk_b], vectors)

        result = retrieve_business_context(
            shared_text, "default", (), settings, vector_store=store, embedding_provider=provider
        )
        content_hashes = {item["source_id"] for item in [i.to_context_dict() for i in result.items]}
        # Only one of the two same-hash chunks should appear.
        assert len(result.items) == 1
        assert content_hashes.issubset({chunk_a.source_id, chunk_b.source_id})


class TestRoleBasedFiltering:
    def test_restricted_chunk_hidden_without_matching_role(self, tmp_path: Path):
        settings = _settings(tmp_path, retrieval_similarity_threshold=0.0)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider(dimensions=16)

        public_chunk = _chunk(
            ChunkType.GLOSSARY, "public_term", "Public glossary term about sales."
        )
        restricted_chunk = _chunk(
            ChunkType.GLOSSARY,
            "restricted_term",
            "Restricted glossary term about executive compensation.",
            allowed_roles=("admin",),
        )
        vectors = provider.embed_batch([public_chunk.text, restricted_chunk.text])
        store.upsert_documents("default", [public_chunk, restricted_chunk], vectors)

        # Query with the restricted chunk's own text -- guarantees a
        # self-similarity of 1.0 (always clears the threshold), since
        # FakeEmbeddingProvider's vectors aren't semantically meaningful for
        # arbitrary text pairs (see its own docstring) and this test is
        # about role filtering, not similarity ranking.
        query = restricted_chunk.text
        as_viewer = retrieve_business_context(
            query, "default", ("viewer",), settings, vector_store=store, embedding_provider=provider
        )
        as_admin = retrieve_business_context(
            query, "default", ("admin",), settings, vector_store=store, embedding_provider=provider
        )

        viewer_ids = {item.chunk.chunk_id for item in as_viewer.items}
        admin_ids = {item.chunk.chunk_id for item in as_admin.items}
        assert restricted_chunk.chunk_id not in viewer_ids
        assert restricted_chunk.chunk_id in admin_ids

    def test_client_supplied_roles_are_never_the_source_of_truth(self, tmp_path: Path):
        """The only role source retrieve_business_context ever consults is
        the caller_roles argument -- there is no filters/roles kwarg a
        caller could pass to override visibility, by construction."""
        import inspect

        signature = inspect.signature(retrieve_business_context)
        assert "caller_roles" in signature.parameters
        assert "filters" not in signature.parameters


class TestContextBudget:
    def test_trims_to_max_context_chars(self, tmp_path: Path):
        settings = _settings(
            tmp_path, retrieval_max_context_chars=120, retrieval_max_context_tokens=1000
        )
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider(dimensions=16)

        chunks = [
            _chunk(ChunkType.DOCUMENTATION, f"doc{i}", "x" * 100, content_hash=f"h{i}")
            for i in range(5)
        ]
        vectors = provider.embed_batch([c.text for c in chunks])
        store.upsert_documents("default", chunks, vectors)

        result = retrieve_business_context(
            "x", "default", (), settings, vector_store=store, embedding_provider=provider
        )
        total_chars = sum(len(item.chunk.text) for item in result.items)
        # Budget is 120 chars; each chunk is 100 chars, so at most one full
        # chunk plus the "always keep the first" allowance should survive.
        assert len(result.items) <= 2
        assert total_chars < 100 * 5


class TestVectorSearchFinds:
    def test_returns_no_results_when_index_empty(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        store.create_collection_if_missing("default")
        provider = FakeEmbeddingProvider(dimensions=16)

        result = retrieve_business_context(
            "anything", "default", (), settings, vector_store=store, embedding_provider=provider
        )
        assert result.items == []
        assert result.warnings  # empty-index warning present


class TestReranker:
    def test_type_priority_breaks_a_tie_in_vector_similarity(self):
        metric_chunk = _chunk(ChunkType.METRIC, "m1", "some metric text with no keyword overlap")
        doc_chunk = _chunk(ChunkType.DOCUMENTATION, "d1", "some documentation text with no overlap")
        candidates = [
            ScoredChunk(chunk=doc_chunk, vector_similarity=0.5),
            ScoredChunk(chunk=metric_chunk, vector_similarity=0.5),
        ]
        ranked = rerank(candidates, "irrelevant question", rerank_weight=0.5, diversity_weight=0.0)
        # Equal vector similarity -> metric's higher type priority should win.
        assert ranked[0].chunk.chunk_type == ChunkType.METRIC

    def test_exact_term_match_gives_a_bonus(self):
        matching = _chunk(ChunkType.DOCUMENTATION, "d1", "This mentions widgets explicitly.")
        nonmatching = _chunk(ChunkType.DOCUMENTATION, "d2", "This is unrelated filler content.")
        candidates = [
            ScoredChunk(chunk=nonmatching, vector_similarity=0.5),
            ScoredChunk(chunk=matching, vector_similarity=0.5),
        ]
        # Every extracted keyword ("explain"/"widgets"/"please") appears only
        # in `matching`'s text, so the exact-match bonus applies to it alone.
        ranked = rerank(
            candidates, "explain widgets please", rerank_weight=0.5, diversity_weight=0.0
        )
        assert ranked[0].chunk.chunk_id == matching.chunk_id

    def test_diversity_penalizes_repeated_table_and_type(self):
        same_table_chunks = [
            _chunk(ChunkType.COLUMN, f"col{i}", f"column {i} text", table_name="FactSales")
            for i in range(4)
        ]
        different_table_chunk = _chunk(
            ChunkType.COLUMN,
            "other_col",
            "a column from a different table",
            table_name="DimProduct",
        )
        candidates = [ScoredChunk(chunk=c, vector_similarity=0.9) for c in same_table_chunks] + [
            ScoredChunk(chunk=different_table_chunk, vector_similarity=0.85)
        ]

        ranked_no_diversity = rerank(candidates, "q", rerank_weight=1.0, diversity_weight=0.0)
        ranked_with_diversity = rerank(candidates, "q", rerank_weight=1.0, diversity_weight=0.5)

        # Without diversity, the highest raw-similarity table dominates every slot.
        assert all(c.chunk.table_name == "FactSales" for c in ranked_no_diversity[:4])
        # With diversity, the different-table chunk should surface earlier
        # than it would purely by similarity rank (it was last by raw score).
        with_diversity_position = next(
            i for i, c in enumerate(ranked_with_diversity) if c.chunk.table_name == "DimProduct"
        )
        assert with_diversity_position < 4

    def test_every_result_gets_a_final_score(self):
        chunks = [_chunk(ChunkType.TABLE, f"t{i}", f"table {i}") for i in range(3)]
        candidates = [ScoredChunk(chunk=c, vector_similarity=0.4) for c in chunks]
        ranked = rerank(candidates, "q", rerank_weight=0.5, diversity_weight=0.2)
        assert all(item.final_score is not None for item in ranked)


class TestKnowledgeChunksHelper:
    def test_build_knowledge_chunks_uses_configured_dimensions(self, tmp_path: Path):
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        (knowledge_dir / "glossary.yaml").write_text(
            "terms:\n  - term: t\n    definition: d\n", encoding="utf-8"
        )
        chunks = build_knowledge_chunks("default", "fake-model", 16, knowledge_dir, 500, 50)
        assert all(c.embedding_dimensions == 16 for c in chunks)
