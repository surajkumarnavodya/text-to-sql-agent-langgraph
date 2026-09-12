"""Unit tests for the golden-dataset store (embeddings/golden_examples.py).

Mirrors tests/test_schema_retriever.py's mocked-Chroma style: these mock the
Chroma collection so they run fast, offline, and without a real embedding
backend -- they check the storage/retrieval *logic* (id determinism, top_k,
similarity filtering, fail-open error handling), not the embedding model.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from embeddings.golden_examples import (
    _collection_name,
    _example_id,
    retrieve_golden_examples,
    save_golden_example,
)


def _mock_settings(top_k: int = 3, min_similarity: float = 0.75) -> MagicMock:
    settings = MagicMock()
    settings.golden_examples_top_k = top_k
    settings.golden_examples_min_similarity = min_similarity
    return settings


class TestCollectionName:
    def test_scoped_per_database(self):
        assert _collection_name("sales") == "golden_examples__sales"
        assert _collection_name("hr") == "golden_examples__hr"
        assert _collection_name("sales") != _collection_name("hr")


class TestExampleId:
    def test_deterministic_for_the_same_triple(self):
        id1 = _example_id("default", "how many orders?", "SELECT COUNT(*) FROM orders")
        id2 = _example_id("default", "how many orders?", "SELECT COUNT(*) FROM orders")
        assert id1 == id2

    def test_differs_across_database_question_or_sql(self):
        base = _example_id("default", "q", "SELECT 1")
        assert base != _example_id("other_db", "q", "SELECT 1")
        assert base != _example_id("default", "different question", "SELECT 1")
        assert base != _example_id("default", "q", "SELECT 2")


class TestSaveGoldenExample:
    def test_upserts_with_deterministic_id_and_sql_in_metadata(self, monkeypatch):
        mock_collection = MagicMock()
        monkeypatch.setattr(
            "embeddings.golden_examples.get_chroma_client", lambda settings: MagicMock()
        )
        monkeypatch.setattr(
            "embeddings.golden_examples.get_golden_examples_collection",
            lambda client, settings, db_name: mock_collection,
        )

        save_golden_example(
            "how many orders?", "SELECT COUNT(*) FROM orders", "default", _mock_settings()
        )

        mock_collection.upsert.assert_called_once()
        _, kwargs = mock_collection.upsert.call_args
        assert kwargs["ids"] == [
            _example_id("default", "how many orders?", "SELECT COUNT(*) FROM orders")
        ]
        assert kwargs["documents"] == ["how many orders?"]
        assert kwargs["metadatas"] == [{"sql": "SELECT COUNT(*) FROM orders"}]

    def test_re_saving_the_same_pair_is_idempotent(self, monkeypatch):
        """Re-clicking the feedback widget (or a Streamlit rerun re-firing
        the same click) must upsert the same document, never accumulate a
        second one -- see save_golden_example's docstring."""
        mock_collection = MagicMock()
        monkeypatch.setattr(
            "embeddings.golden_examples.get_chroma_client", lambda settings: MagicMock()
        )
        monkeypatch.setattr(
            "embeddings.golden_examples.get_golden_examples_collection",
            lambda client, settings, db_name: mock_collection,
        )

        save_golden_example("q", "SELECT 1", "default", _mock_settings())
        save_golden_example("q", "SELECT 1", "default", _mock_settings())

        first_id = mock_collection.upsert.call_args_list[0].kwargs["ids"]
        second_id = mock_collection.upsert.call_args_list[1].kwargs["ids"]
        assert first_id == second_id

    def test_never_raises_on_a_chroma_failure(self, monkeypatch):
        monkeypatch.setattr(
            "embeddings.golden_examples.get_chroma_client",
            lambda settings: (_ for _ in ()).throw(RuntimeError("chroma unavailable")),
        )

        # Must not raise -- a storage failure must not surface as a broken
        # UI feedback click.
        save_golden_example("q", "SELECT 1", "default", _mock_settings())


class TestRetrieveGoldenExamples:
    def test_returns_examples_above_the_similarity_threshold(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 2
        mock_collection.query.return_value = {
            "documents": [["how many orders?", "how many customers?"]],
            "metadatas": [
                [{"sql": "SELECT COUNT(*) FROM orders"}, {"sql": "SELECT COUNT(*) FROM customers"}]
            ],
            "distances": [[0.1, 0.5]],  # similarity 0.9 and 0.5
        }
        monkeypatch.setattr(
            "embeddings.golden_examples.get_chroma_client", lambda settings: MagicMock()
        )
        monkeypatch.setattr(
            "embeddings.golden_examples.get_golden_examples_collection",
            lambda client, settings, db_name: mock_collection,
        )

        examples = retrieve_golden_examples(
            "how many orders?", "default", settings=_mock_settings(min_similarity=0.75)
        )

        assert len(examples) == 1
        assert examples[0]["question"] == "how many orders?"
        assert examples[0]["sql"] == "SELECT COUNT(*) FROM orders"
        assert examples[0]["similarity_score"] == 0.9

    def test_respects_top_k(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 5
        mock_collection.query.return_value = {
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        }
        monkeypatch.setattr(
            "embeddings.golden_examples.get_chroma_client", lambda settings: MagicMock()
        )
        monkeypatch.setattr(
            "embeddings.golden_examples.get_golden_examples_collection",
            lambda client, settings, db_name: mock_collection,
        )

        retrieve_golden_examples("q", "default", settings=_mock_settings(top_k=2))

        _, kwargs = mock_collection.query.call_args
        assert kwargs["n_results"] == 2

    def test_empty_collection_returns_no_examples(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        monkeypatch.setattr(
            "embeddings.golden_examples.get_chroma_client", lambda settings: MagicMock()
        )
        monkeypatch.setattr(
            "embeddings.golden_examples.get_golden_examples_collection",
            lambda client, settings, db_name: mock_collection,
        )

        assert retrieve_golden_examples("q", "default", settings=_mock_settings()) == []
        mock_collection.query.assert_not_called()

    def test_never_raises_on_a_chroma_failure(self, monkeypatch):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 3
        mock_collection.query.side_effect = RuntimeError("embedding backend exploded")
        monkeypatch.setattr(
            "embeddings.golden_examples.get_chroma_client", lambda settings: MagicMock()
        )
        monkeypatch.setattr(
            "embeddings.golden_examples.get_golden_examples_collection",
            lambda client, settings, db_name: mock_collection,
        )

        assert retrieve_golden_examples("q", "default", settings=_mock_settings()) == []
