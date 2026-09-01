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

from typing import Any
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


class TestKeywordMatchExpansion:
    """Regression coverage for a real, reproduced failure: DimProduct ranked
    outside the top 15 of 31 tables (pure vector similarity) for a question
    containing the word "product," because its embedded DDL is dominated by
    nine language-variant description columns -- causing the agent to
    invent wrong column names for a table it was never shown. See
    `embeddings/retriever.py::_expand_with_keyword_matches`.
    """

    def _mock_settings(self, schema_top_k=2):
        settings = MagicMock()
        settings.schema_top_k = schema_top_k
        return settings

    def _mock_collection(self, top_k_result, all_metadatas, documents_by_id):
        collection = MagicMock()
        collection.count.return_value = len(all_metadatas)
        collection.query.return_value = top_k_result

        def _get(ids=None, include=None):
            if ids is not None:
                return {
                    "ids": ids,
                    "documents": [documents_by_id[i] for i in ids],
                }
            return {"metadatas": all_metadatas}

        collection.get.side_effect = _get
        return collection

    def test_table_matching_a_distinctive_keyword_is_added(self, monkeypatch):
        # Vector similarity only returns FactInternetSales -- DimProduct
        # never makes it into the top_k despite "product" being a literal
        # substring of its name.
        top_k_result = {
            "documents": [["CREATE TABLE FactInternetSales (...)"]],
            "metadatas": [[{"table_name": "FactInternetSales", "fk_targets": ""}]],
            "distances": [[0.2]],
        }
        all_metadatas = [
            {"table_name": "FactInternetSales", "fk_targets": ""},
            {"table_name": "DimProduct", "fk_targets": ""},
            {"table_name": "DimCurrency", "fk_targets": ""},
        ]
        documents_by_id = {"DimProduct": "CREATE TABLE DimProduct (...)"}
        mock_collection = self._mock_collection(top_k_result, all_metadatas, documents_by_id)
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection", lambda client, settings: mock_collection
        )

        tables = retrieve_relevant_schema(
            "Show total sales by year and product name.", settings=self._mock_settings()
        )

        names = [t["table_name"] for t in tables]
        assert "DimProduct" in names
        assert "DimCurrency" not in names  # no keyword match, no vector match -- must not appear

    def test_no_keyword_match_leaves_result_unchanged(self, monkeypatch):
        top_k_result = {
            "documents": [["CREATE TABLE FactInternetSales (...)"]],
            "metadatas": [[{"table_name": "FactInternetSales", "fk_targets": ""}]],
            "distances": [[0.2]],
        }
        all_metadatas = [
            {"table_name": "FactInternetSales", "fk_targets": ""},
            {"table_name": "DimProduct", "fk_targets": ""},
        ]
        mock_collection = self._mock_collection(top_k_result, all_metadatas, {})
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection", lambda client, settings: mock_collection
        )

        tables = retrieve_relevant_schema(
            "What is the average order value?", settings=self._mock_settings()
        )

        assert [t["table_name"] for t in tables] == ["FactInternetSales"]

    def test_matches_are_capped_at_the_budget(self, monkeypatch):
        top_k_result: dict[str, list[list[Any]]] = {
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }
        # Five tables all contain "product" -- only _MAX_KEYWORD_MATCHES (3)
        # should be added, picked deterministically (alphabetical).
        table_names = [
            "DimProduct",
            "DimProductCategory",
            "DimProductSubcategory",
            "FactProductInventory",
            "FactAdditionalInternationalProductDescription",
        ]
        all_metadatas = [{"table_name": name, "fk_targets": ""} for name in table_names]
        documents_by_id = {name: f"CREATE TABLE {name} (...)" for name in table_names}
        mock_collection = self._mock_collection(top_k_result, all_metadatas, documents_by_id)
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection", lambda client, settings: mock_collection
        )

        tables = retrieve_relevant_schema("product name", settings=self._mock_settings())

        assert len(tables) == 3
        assert [t["table_name"] for t in tables] == [
            "DimProduct",
            "DimProductCategory",
            "DimProductSubcategory",
        ]

    def test_short_and_stopword_terms_never_trigger_a_match(self, monkeypatch):
        top_k_result: dict[str, list[list[Any]]] = {
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }
        all_metadatas = [
            {"table_name": "FactSales", "fk_targets": ""},
            {"table_name": "DimDate", "fk_targets": ""},
        ]
        mock_collection = self._mock_collection(top_k_result, all_metadatas, {})
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection", lambda client, settings: mock_collection
        )

        # "sales", "date", "total" are all stopwords for this fallback
        # (vector similarity already handles these broad terms well).
        tables = retrieve_relevant_schema(
            "What was the total sales by date?", settings=self._mock_settings()
        )

        assert tables == []
