"""Fallback-behavior tests -- the app must keep working when vector
retrieval is unavailable for any reason. See retrieval/retriever.py's
module docstring and docs/vector-retrieval-design.md's "Fallback behavior"
section for the full contract this asserts.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from retrieval.embeddings import EmbeddingError, FakeEmbeddingProvider
from retrieval.retriever import retrieve_business_context
from retrieval.vector_store import HealthCheckResult, VectorStoreError

from config.settings import Settings
from tests._retrieval_fakes import InMemoryVectorStore


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "retrieval_embedding_provider": "fake",
        "retrieval_knowledge_dir": tmp_path,
    }
    base.update(overrides)
    return Settings(**base)


class TestFeatureDisabled:
    def test_returns_empty_result_with_no_warning(self, tmp_path: Path):
        settings = _settings(tmp_path, enable_business_context_retrieval=False)
        result = retrieve_business_context("anything", "default", (), settings)
        assert result.items == []
        assert result.warnings == []  # a disabled feature is not a failure
        assert result.metadata == {"enabled": False}


class TestMissingCollection:
    def test_empty_index_returns_empty_result_with_a_warning(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        store.create_collection_if_missing("default")  # exists, but empty
        provider = FakeEmbeddingProvider()

        result = retrieve_business_context(
            "anything", "default", (), settings, vector_store=store, embedding_provider=provider
        )
        assert result.items == []
        assert result.warnings
        assert "empty" in result.warnings[0].lower()


class TestVectorStoreUnhealthy:
    def test_health_check_failure_degrades_gracefully(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        store.fail_health_check = True
        provider = FakeEmbeddingProvider()

        result = retrieve_business_context(
            "anything", "default", (), settings, vector_store=store, embedding_provider=provider
        )
        assert result.items == []
        assert result.warnings
        assert result.metadata.get("health_ok") is False


class TestVectorStoreQueryFailure:
    def test_query_failure_degrades_gracefully(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider(dimensions=8)
        # Seed one chunk so health_check reports chunk_count > 0 and the
        # code path actually reaches the failing query call.
        from retrieval.models import Chunk, ChunkType, make_chunk_id

        chunk = Chunk(
            chunk_id=make_chunk_id("default", None, "T", ChunkType.TABLE),
            chunk_type=ChunkType.TABLE,
            text="table T",
            database_id="default",
            source_id="table:T",
            content_hash="h",
            embedding_model="fake",
            embedding_dimensions=8,
        )
        store.upsert_documents("default", [chunk], provider.embed_batch([chunk.text]))
        store.fail_query = True

        result = retrieve_business_context(
            "anything", "default", (), settings, vector_store=store, embedding_provider=provider
        )
        assert result.items == []
        assert result.warnings
        assert result.metadata.get("error") is True


class TestEmbeddingFailure:
    def test_embedding_provider_failure_degrades_gracefully(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        # Seed at least one chunk so health_check reports a non-empty index,
        # reaching the embed_text() call that then fails.
        store.create_collection_if_missing("default")
        store._data["default"]["dummy"] = (  # noqa: SLF001 - direct seed for the test
            MagicMock(chunk_type=None),
            [0.1],
        )

        failing_provider = MagicMock()
        failing_provider.embed_text.side_effect = EmbeddingError("simulated embedding failure")

        result = retrieve_business_context(
            "anything",
            "default",
            (),
            settings,
            vector_store=store,
            embedding_provider=failing_provider,
        )
        assert result.items == []
        assert result.warnings
        assert "unavailable" in result.warnings[0].lower() or "failed" in result.warnings[0].lower()


class TestMissingEmbeddingCredentials:
    def test_unrecognized_provider_raises_a_clear_configuration_error(self, tmp_path: Path):
        """Not a runtime fallback case -- an unrecognized provider name is a
        configuration mistake caught at factory time (see
        retrieval.embeddings.get_embedding_provider's docstring), not
        something retrieval silently degrades around."""
        import pytest
        from retrieval.embeddings import get_embedding_provider

        settings = _settings(tmp_path)
        object.__setattr__(settings, "retrieval_embedding_provider", "nonexistent")
        with pytest.raises(ValueError, match="Unrecognized"):
            get_embedding_provider(settings)


class TestVectorStoreErrorNeverPropagates:
    def test_retrieve_business_context_never_raises(self, tmp_path: Path):
        """The top-level contract: whatever goes wrong inside retrieval,
        retrieve_business_context itself must never raise -- the caller
        (agent.nodes.retrieve_business_context_node) has its own
        defense-in-depth try/except, but this is the real guarantee."""
        settings = _settings(tmp_path)

        class ExplodingStore(InMemoryVectorStore):
            def health_check(self, database_id: str) -> HealthCheckResult:
                raise VectorStoreError("boom")

        store = ExplodingStore()
        provider = FakeEmbeddingProvider()

        result = retrieve_business_context(
            "anything", "default", (), settings, vector_store=store, embedding_provider=provider
        )
        assert result.items == []
        assert result.warnings


class TestSqlAgentContinuesWithoutRetrieval:
    def test_generate_sql_prompt_omits_business_context_block_when_empty(self):
        """The generation prompt must be well-formed with no business-context
        section at all when retrieval found nothing -- never a broken/empty
        section placeholder that could confuse the model."""
        from agent.llm_client import _build_user_prompt

        prompt = _build_user_prompt(
            question="how many orders?",
            schema_context="CREATE TABLE orders (...)",
            previous_sql=None,
            error_feedback=None,
            retrieved_context=None,
        )
        assert "Retrieved business context" not in prompt

        prompt_empty_list = _build_user_prompt(
            question="how many orders?",
            schema_context="CREATE TABLE orders (...)",
            previous_sql=None,
            error_feedback=None,
            retrieved_context=[],
        )
        assert "Retrieved business context" not in prompt_empty_list
