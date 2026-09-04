"""Unit tests for multi-database auto-routing.

Covers `embeddings.retriever.select_database` (which configured database a
question is routed to) and `agent.nodes.retrieve_schema_node`'s "a retry
reuses the already-selected database, never re-routes" contract. Fully
mocked -- no real ChromaDB, database, or embedding backend.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.exceptions import SchemaRetrievalError
from agent.nodes import retrieve_schema_node
from config.settings import Settings
from embeddings.retriever import DatabaseSelection, select_database
from security.secrets import SecretStr

_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="postgresql",
    db_host="db.example.com",
    db_port=None,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("secret"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="ODBC Driver 17 for SQL Server",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="schema_ddl",
    embedding_model_name="all-MiniLM-L6-v2",
    schema_top_k=4,
    max_retries=3,
    max_result_rows=1000,
    query_timeout_seconds=15,
    llm_max_tokens=1024,
    insight_max_tokens=120,
    max_question_length=500,
    question_rate_limit_per_minute=10,
    llm_call_rate_limit_per_minute=20,
    cost_estimation_enabled=True,
    cost_estimation_timeout_seconds=3,
    cost_moderate_row_threshold=50_000,
    cost_high_row_threshold=1_000_000,
    log_level="INFO",
    log_redaction_level="standard",
)


def _two_db_settings() -> Settings:
    """A Settings with two named connections ("sales", "hr"), same shape
    otherwise -- for exercising select_database's actual comparison logic
    (the single-database case short-circuits and never reaches it)."""
    sales = dataclasses.replace(_BASE_SETTINGS.databases[0], name="sales")
    hr = dataclasses.replace(_BASE_SETTINGS.databases[0], name="hr")
    return dataclasses.replace(_BASE_SETTINGS, databases=(sales, hr))


def _mock_collection(score: float | None, count: int = 5) -> MagicMock:
    """A fake per-database Chroma collection with a fixed top-1 score.

    `score=None` means "this database's index isn't built" (count=0,
    `.query` never meaningfully called).
    """
    collection = MagicMock()
    collection.count.return_value = 0 if score is None else count
    if score is not None:
        collection.query.return_value = {"distances": [[1.0 - score]]}
    return collection


class TestSelectDatabaseSingleDbShortCircuit:
    def test_short_circuits_without_ever_touching_chroma(self, monkeypatch):
        touched = []
        monkeypatch.setattr(
            "embeddings.retriever.get_chroma_client", lambda settings: touched.append(1)
        )

        selection = select_database("any question", settings=_BASE_SETTINGS)

        assert selection.db_name == "default"
        assert selection.top_table_score == 1.0
        assert selection.scores_by_db == {"default": 1.0}
        assert touched == []


class TestSelectDatabaseMultiDb:
    def test_picks_the_database_with_the_higher_top_table_score(self, monkeypatch):
        settings = _two_db_settings()
        collections = {"sales": _mock_collection(0.8), "hr": _mock_collection(0.3)}
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection",
            lambda client, settings, db_name: collections[db_name],
        )

        selection = select_database("What is the total sales amount?", settings=settings)

        assert selection.db_name == "sales"
        assert selection.top_table_score == 0.8
        assert selection.scores_by_db == {"sales": 0.8, "hr": 0.3}

    def test_skips_a_database_whose_index_is_not_built_yet(self, monkeypatch):
        settings = _two_db_settings()
        collections = {"sales": _mock_collection(None), "hr": _mock_collection(0.6)}
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection",
            lambda client, settings, db_name: collections[db_name],
        )

        selection = select_database("any question", settings=settings)

        assert selection.db_name == "hr"
        assert "sales" not in selection.scores_by_db

    def test_one_databases_query_failing_does_not_block_routing_to_the_other(self, monkeypatch):
        settings = _two_db_settings()

        def _get_collection(client, settings, db_name):
            if db_name == "sales":
                raise RuntimeError("chroma backend exploded")
            return _mock_collection(0.4)

        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr("embeddings.retriever.get_collection", _get_collection)

        selection = select_database("any question", settings=settings)

        assert selection.db_name == "hr"
        assert "sales" not in selection.scores_by_db

    def test_raises_when_every_configured_database_index_is_empty(self, monkeypatch):
        settings = _two_db_settings()
        empty = _mock_collection(None)
        monkeypatch.setattr("embeddings.retriever.get_chroma_client", lambda settings: MagicMock())
        monkeypatch.setattr(
            "embeddings.retriever.get_collection", lambda client, settings, db_name: empty
        )

        with pytest.raises(SchemaRetrievalError):
            select_database("any question", settings=settings)


@pytest.fixture(autouse=True)
def _mock_settings_for_node_tests(monkeypatch):
    """`retrieve_schema_node` calls `agent.nodes.get_settings()` -- see the
    identical fixture in tests/test_agent_nodes.py, duplicated here per this
    project's existing per-file convention rather than a shared import."""
    monkeypatch.setattr("agent.nodes.get_settings", lambda: _BASE_SETTINGS)
    return _BASE_SETTINGS


class TestRetrieveSchemaNodeDatabaseRouting:
    def test_first_pass_calls_select_database_and_records_the_choice(self, monkeypatch):
        monkeypatch.setattr(
            "agent.nodes.select_database",
            lambda question, settings: DatabaseSelection(
                db_name="hr", top_table_score=0.9, scores_by_db={"hr": 0.9}
            ),
        )
        monkeypatch.setattr(
            "agent.nodes.retrieve_relevant_schema", lambda question, db_name, top_k: []
        )

        result = retrieve_schema_node({"question": "list employees", "retry_count": 0})

        assert result["selected_database"] == "hr"

    def test_missing_reference_retry_reuses_the_previously_selected_database(self, monkeypatch):
        """The `execute_sql` -> `retrieve_schema` retry path must keep
        targeting the same database attempt 1 already generated/executed
        SQL against -- select_database must not be called again."""
        select_database_calls = []
        monkeypatch.setattr(
            "agent.nodes.select_database",
            lambda question, settings: select_database_calls.append(1),
        )
        captured = {}

        def _capture(question, db_name, top_k):
            captured["db_name"] = db_name
            return []

        monkeypatch.setattr("agent.nodes.retrieve_relevant_schema", _capture)

        result = retrieve_schema_node(
            {
                "question": "list employees",
                "retry_count": 1,
                "error_history": ["SQL execution error: Invalid column name 'Foo'."],
                "selected_database": "hr",
            }
        )

        assert select_database_calls == []
        assert captured["db_name"] == "hr"
        assert result["selected_database"] == "hr"
