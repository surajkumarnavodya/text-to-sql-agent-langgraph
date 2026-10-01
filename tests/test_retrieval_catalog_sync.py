"""Unit tests for retrieval/ingestion.py's
`sync_catalog_entry_to_vector_store`/`business_concept_chunk_id_for_snapshot`
(Prompt 09, `09_SEMANTIC_CATALOG_CONTRACT.md`).

Uses `retrieval.embeddings.FakeEmbeddingProvider` (deterministic, no
network/model) and `tests._retrieval_fakes.InMemoryVectorStore` (no
chromadb, no disk I/O) -- the same convention `tests/test_ingestion.py`
already establishes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from retrieval.embeddings import FakeEmbeddingProvider
from retrieval.ingestion import (
    business_concept_chunk_id_for_snapshot,
    sync_catalog_entry_to_vector_store,
)
from retrieval.vector_store import VectorStoreError
from semantic.catalog import CatalogConceptType, CatalogEntrySnapshot, CatalogStatus

from config.settings import Settings
from tests._retrieval_fakes import InMemoryVectorStore


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(exist_ok=True)
    base: dict[str, Any] = {
        "retrieval_embedding_provider": "fake",
        "retrieval_knowledge_dir": knowledge_dir,
    }
    base.update(overrides)
    return Settings(**base)


def _snapshot(**overrides: Any) -> CatalogEntrySnapshot:
    defaults: dict[str, Any] = dict(
        id="entry-1",
        tenant_id="tenant-a",
        database_id="db1",
        concept_type=CatalogConceptType.METRIC,
        concept_key="clv",
        business_name="Customer Lifetime Value",
        status=CatalogStatus.PUBLISHED,
        version=1,
    )
    defaults.update(overrides)
    return CatalogEntrySnapshot(**defaults)


class TestSyncUpsertsTheNewChunk:
    def test_upserts_exactly_one_chunk_for_the_database(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        sync_catalog_entry_to_vector_store(
            _snapshot(), settings, vector_store=store, embedding_provider=provider
        )

        assert store.count("db1") == 1

    def test_returned_chunk_id_matches_the_deterministic_helper(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()
        snapshot = _snapshot()

        chunk = sync_catalog_entry_to_vector_store(
            snapshot, settings, vector_store=store, embedding_provider=provider
        )

        assert chunk.chunk_id == business_concept_chunk_id_for_snapshot(snapshot)

    def test_creates_the_collection_if_missing(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()
        assert store.health_check("db1").chunk_count == 0

        sync_catalog_entry_to_vector_store(
            _snapshot(), settings, vector_store=store, embedding_provider=provider
        )

        assert store.health_check("db1").chunk_count == 1


class TestSupersessionRemovesTheOldChunk:
    def test_the_superseded_chunk_id_is_deleted_and_the_new_one_is_present(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        v1 = _snapshot(version=1)
        v1_chunk = sync_catalog_entry_to_vector_store(
            v1, settings, vector_store=store, embedding_provider=provider
        )
        assert store.count("db1") == 1

        v2 = _snapshot(version=2, business_name="Customer Lifetime Value v2")
        sync_catalog_entry_to_vector_store(
            v2,
            settings,
            superseded_chunk_id=v1_chunk.chunk_id,
            vector_store=store,
            embedding_provider=provider,
        )

        # Still exactly one chunk: v1's removed, v2's added.
        assert store.count("db1") == 1
        remaining = store.similarity_search("db1", provider.embed_text("x"), top_k=10)
        assert [c.chunk.chunk_id for c in remaining] == [business_concept_chunk_id_for_snapshot(v2)]

    def test_no_superseded_chunk_id_means_nothing_extra_is_deleted(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        provider = FakeEmbeddingProvider()

        sync_catalog_entry_to_vector_store(
            _snapshot(), settings, vector_store=store, embedding_provider=provider
        )
        assert store.count("db1") == 1


class TestFailureModes:
    def test_a_vector_store_upsert_failure_raises_vector_store_error(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store = InMemoryVectorStore()
        store.fail_next_upsert = True
        provider = FakeEmbeddingProvider()

        with pytest.raises(VectorStoreError):
            sync_catalog_entry_to_vector_store(
                _snapshot(), settings, vector_store=store, embedding_provider=provider
            )


class TestBusinessConceptChunkIdForSnapshot:
    def test_is_stable_for_the_same_snapshot_identity(self):
        a = business_concept_chunk_id_for_snapshot(_snapshot())
        b = business_concept_chunk_id_for_snapshot(_snapshot())
        assert a == b

    def test_differs_across_versions(self):
        a = business_concept_chunk_id_for_snapshot(_snapshot(version=1))
        b = business_concept_chunk_id_for_snapshot(_snapshot(version=2))
        assert a != b
