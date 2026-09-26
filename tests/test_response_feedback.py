"""Unit tests for the general-purpose response-feedback store
(feedback/store.py).

Mirrors tests/test_golden_examples.py's mocked-Chroma style: these mock the
Chroma collection so they run fast, offline, and without a real embedding
backend -- they check the storage logic (metadata shape, truncation,
fail-open error handling), not the embedding model.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from feedback.store import _MAX_TEXT_LENGTH, get_feedback_collection, save_response_feedback


class TestSaveResponseFeedback:
    def test_stores_rating_and_metadata(self, monkeypatch):
        mock_collection = MagicMock()
        monkeypatch.setattr(
            "feedback.store.get_feedback_collection", lambda settings: mock_collection
        )

        save_response_feedback(
            "how many orders?",
            "There were 42 orders.",
            "positive",
            sql="SELECT COUNT(*) FROM orders",
            database="sales",
            sources_used=["sql"],
            comment="Great work.",
            conversation_id="conv-1",
            settings=MagicMock(),
        )

        mock_collection.add.assert_called_once()
        _, kwargs = mock_collection.add.call_args
        assert kwargs["documents"] == ["how many orders?"]
        metadata = kwargs["metadatas"][0]
        assert metadata["rating"] == "positive"
        assert metadata["answer"] == "There were 42 orders."
        assert metadata["sql"] == "SELECT COUNT(*) FROM orders"
        assert metadata["database"] == "sales"
        assert metadata["sources_used"] == "sql"
        assert metadata["comment"] == "Great work."
        assert metadata["conversation_id"] == "conv-1"
        assert "created_at" in metadata

    def test_negative_rating_with_no_sql_or_comment_is_stored_too(self, monkeypatch):
        """Unlike embeddings.golden_examples (thumbs-up + confirmed SQL
        only), this store must also capture a plain dislike with nothing
        else attached -- that's the whole point of this being additive."""
        mock_collection = MagicMock()
        monkeypatch.setattr(
            "feedback.store.get_feedback_collection", lambda settings: mock_collection
        )

        save_response_feedback(
            "what's our policy on X?",
            "According to policy Y...",
            "negative",
            settings=MagicMock(),
        )

        mock_collection.add.assert_called_once()
        _, kwargs = mock_collection.add.call_args
        metadata = kwargs["metadatas"][0]
        assert metadata["rating"] == "negative"
        assert metadata["sql"] == ""
        assert metadata["database"] == ""
        assert metadata["sources_used"] == ""
        assert metadata["comment"] == ""

    def test_each_call_gets_its_own_id_never_upserted(self, monkeypatch):
        """Unlike golden_examples' deterministic upsert-by-hash id, every
        feedback event is its own log entry -- two identical ratings on the
        same question must not collapse into one record."""
        mock_collection = MagicMock()
        monkeypatch.setattr(
            "feedback.store.get_feedback_collection", lambda settings: mock_collection
        )

        save_response_feedback("q", "a", "positive", settings=MagicMock())
        save_response_feedback("q", "a", "positive", settings=MagicMock())

        first_id = mock_collection.add.call_args_list[0].kwargs["ids"]
        second_id = mock_collection.add.call_args_list[1].kwargs["ids"]
        assert first_id != second_id

    def test_long_comment_and_answer_are_truncated(self, monkeypatch):
        mock_collection = MagicMock()
        monkeypatch.setattr(
            "feedback.store.get_feedback_collection", lambda settings: mock_collection
        )

        save_response_feedback(
            "q",
            "a" * (_MAX_TEXT_LENGTH + 500),
            "negative",
            comment="c" * (_MAX_TEXT_LENGTH + 500),
            settings=MagicMock(),
        )

        _, kwargs = mock_collection.add.call_args
        metadata = kwargs["metadatas"][0]
        assert len(metadata["answer"]) == _MAX_TEXT_LENGTH
        assert len(metadata["comment"]) == _MAX_TEXT_LENGTH

    def test_never_raises_on_a_chroma_failure(self, monkeypatch):
        monkeypatch.setattr(
            "feedback.store.get_feedback_collection",
            lambda settings: (_ for _ in ()).throw(RuntimeError("chroma unavailable")),
        )

        # Must not raise -- a storage failure must not surface as a broken
        # UI feedback click.
        save_response_feedback("q", "a", "positive", settings=MagicMock())


class TestGetFeedbackCollection:
    def test_uses_a_single_shared_collection_name(self, monkeypatch):
        mock_client = MagicMock()
        monkeypatch.setattr("feedback.store.get_chroma_client", lambda settings: mock_client)
        monkeypatch.setattr("feedback.store.get_embedding_function", lambda settings: "ef")

        get_feedback_collection(MagicMock())

        _, kwargs = mock_client.get_or_create_collection.call_args
        assert kwargs["name"] == "response_feedback"
