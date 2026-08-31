"""Unit tests for top-k schema retrieval.

`retrieve_relevant_schema` talks to a real ChromaDB collection + embedding
model in production; that's exercised end-to-end by
`scripts/build_embeddings.py` and `scripts/integration_test.py`, not here.
These tests mock the Chroma collection so they run fast, offline, and
without requiring a real database or the schema index to have been built --
they check the retrieval *logic* (result shaping, top_k, error handling),
not the embedding backend.

(Schema *chunking* -- turning introspected tables into embeddable DDL text
-- is covered by `tests/test_schema_introspection.py`, since that's now
owned by `db/schema_introspection.py`, not this module.)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.exceptions import SchemaRetrievalError
from embeddings.retriever import retrieve_relevant_schema


class TestRetrieveRelevantSchema:
    def _mock_settings(self, schema_top_k=4):
        settings = MagicMock()
        settings.schema_top_k = schema_top_k
        return settings

    def test_returns_top_k_tables_ordered_by_similarity(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 5
        mock_collection.query.return_value = {
            "documents": [["CREATE TABLE orders (...)", "CREATE TABLE customers (...)"]],
            "metadatas": [[{"table_name": "orders"}, {"table_name": "customers"}]],
            "distances": [[0.1, 0.4]],
        }
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection", lambda client, settings: mock_collection
        )

        tables = retrieve_relevant_schema(
            "How many orders per customer?", top_k=2, settings=self._mock_settings()
        )

        assert [t["table_name"] for t in tables] == ["orders", "customers"]
        assert tables[0]["similarity_score"] > tables[1]["similarity_score"]

    def test_raises_when_index_is_empty(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection", lambda client, settings: mock_collection
        )

        with pytest.raises(SchemaRetrievalError):
            retrieve_relevant_schema("any question", settings=self._mock_settings())

    def test_wraps_unexpected_query_errors(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 5
        mock_collection.query.side_effect = RuntimeError("embedding backend exploded")
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection", lambda client, settings: mock_collection
        )

        with pytest.raises(SchemaRetrievalError):
            retrieve_relevant_schema("any question", settings=self._mock_settings())
