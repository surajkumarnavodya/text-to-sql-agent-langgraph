"""Unit tests for individual LangGraph node functions (agent/nodes.py).

Each node is tested in isolation with its external dependencies (LLM calls,
schema retrieval, SQL execution) mocked, so these tests verify the *state
transition contract* of each node -- exactly what should be
explainable/interview-ready about this architecture -- without needing
Ollama, a built Chroma index, or a real database connection.

`_mock_settings` is autouse and patches `agent.nodes.get_settings` to a
fixed, postgres-flavored `Settings` object: these tests must behave
identically regardless of what a developer's real `.env` happens to point
at (this project's `.env` is a real, developer-specific database connection,
not a test fixture) -- validate_sql_node's row-limit rendering
("LIMIT" vs "TOP") in particular depends on `Settings.db_type`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from agent.exceptions import OllamaUnavailableError, SchemaRetrievalError
from agent.nodes import (
    execute_sql_node,
    generate_sql_node,
    retrieve_schema_node,
    route_after_execution,
    route_after_validation,
    validate_sql_node,
)
from agent.state import AgentState
from config.settings import Settings


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    settings = Settings(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        ollama_request_timeout_seconds=60,
        db_type="postgresql",
        db_host="db.example.com",
        db_port=None,
        db_name="mydb",
        db_user="reader",
        db_password="secret",
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
        log_level="INFO",
    )
    monkeypatch.setattr("agent.nodes.get_settings", lambda: settings)
    return settings


class TestRetrieveSchemaNode:
    def test_populates_schema_context_on_success(self, monkeypatch):
        fake_tables = [
            {"table_name": "orders", "ddl": "CREATE TABLE orders (...)", "similarity_score": 0.9},
            {
                "table_name": "customers",
                "ddl": "CREATE TABLE customers (...)",
                "similarity_score": 0.7,
            },
        ]
        monkeypatch.setattr(
            "agent.nodes.retrieve_relevant_schema", lambda question, top_k: fake_tables
        )

        result = retrieve_schema_node({"question": "top customers by orders"})

        assert result["status"] == "generating"
        assert result["schema_tables"] == fake_tables
        assert "CREATE TABLE orders" in result["schema_context_text"]
        assert "CREATE TABLE customers" in result["schema_context_text"]

    def test_marks_failed_on_schema_retrieval_error(self, monkeypatch):
        def _raise(question, top_k):
            raise SchemaRetrievalError("index not built")

        monkeypatch.setattr("agent.nodes.retrieve_relevant_schema", _raise)

        result = retrieve_schema_node({"question": "anything", "retry_count": 0})

        assert result["status"] == "failed"
        assert "index not built" in result["error_history"][0]
        assert result["attempt_history"][0]["outcome"] == "schema_retrieval_error"
        assert "index not built" in result["failure_explanation"]

    def test_first_call_uses_plain_question_and_default_top_k(self, monkeypatch):
        captured = {}

        def _capture(question, top_k):
            captured["question"] = question
            captured["top_k"] = top_k
            return []

        monkeypatch.setattr("agent.nodes.retrieve_relevant_schema", _capture)

        retrieve_schema_node({"question": "how many customers", "retry_count": 0})

        assert captured["question"] == "how many customers"
        assert captured["top_k"] == 4  # settings.schema_top_k, unwidened

    def test_retry_after_missing_reference_broadens_query_and_top_k(self, monkeypatch):
        """On a schema-retry (retry_count > 0, error history present), the
        retrieval query should fold in the actual DB error and widen top_k --
        this is what lets a re-retrieval surface different tables than the
        first attempt did, rather than repeating the exact same search."""
        captured = {}

        def _capture(question, top_k):
            captured["question"] = question
            captured["top_k"] = top_k
            return []

        monkeypatch.setattr("agent.nodes.retrieve_relevant_schema", _capture)

        retrieve_schema_node(
            {
                "question": "how many customers",
                "retry_count": 1,
                "error_history": ["SQL execution error: Invalid column name 'CustName'."],
            }
        )

        assert "how many customers" in captured["question"]
        assert "Invalid column name" in captured["question"]
        assert captured["top_k"] == 6  # schema_top_k (4) + 2


class TestGenerateSqlNode:
    def test_sets_sql_and_advances_to_validating(self, monkeypatch):
        monkeypatch.setattr(
            "agent.nodes.generate_sql_from_llm",
            lambda **kwargs: "SELECT * FROM customers",
        )

        state: AgentState = {
            "question": "list customers",
            "schema_context_text": "CREATE TABLE customers (...)",
            "error_history": [],
            "retry_count": 0,
        }
        result = generate_sql_node(state)

        assert result["status"] == "validating"
        assert result["sql"] == "SELECT * FROM customers"

    def test_marks_failed_when_ollama_unavailable(self, monkeypatch):
        def _raise(**kwargs):
            raise OllamaUnavailableError("connection refused")

        monkeypatch.setattr("agent.nodes.generate_sql_from_llm", _raise)

        state: AgentState = {
            "question": "list customers",
            "schema_context_text": "",
            "error_history": [],
            "retry_count": 0,
        }
        result = generate_sql_node(state)

        assert result["status"] == "failed"
        assert "connection refused" in result["error_history"][0]
        assert result["attempt_history"][0]["outcome"] == "llm_error"
        assert result["failure_explanation"] is not None

    def test_includes_last_error_from_history_in_llm_call(self, monkeypatch):
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return "SELECT 1"

        monkeypatch.setattr("agent.nodes.generate_sql_from_llm", _capture)

        state: AgentState = {
            "question": "q",
            "schema_context_text": "",
            "error_history": ["SQL validation error: DROP is not allowed"],
            "retry_count": 1,
            "sql": "DROP TABLE customers",
        }
        generate_sql_node(state)

        assert captured["error_feedback"] == "SQL validation error: DROP is not allowed"
        assert captured["previous_sql"] == "DROP TABLE customers"

    def test_forwards_error_category_to_llm_call(self, monkeypatch):
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return "SELECT 1"

        monkeypatch.setattr("agent.nodes.generate_sql_from_llm", _capture)

        state: AgentState = {
            "question": "q",
            "schema_context_text": "",
            "error_history": ["SQL execution error: Invalid column name 'Foo'."],
            "retry_count": 1,
            "sql": "SELECT Foo FROM customers",
            "last_error_category": "missing_reference",
        }
        generate_sql_node(state)

        assert captured["error_category"] == "missing_reference"


class TestValidateSqlNode:
    def test_valid_sql_advances_to_executing(self):
        state: AgentState = {"sql": "SELECT * FROM customers", "retry_count": 0}
        result = validate_sql_node(state)

        assert result["status"] == "executing"
        assert result["validation_error"] is None
        assert "LIMIT" in result["sql"].upper()

    def test_parse_error_retries_when_under_max_retries(self):
        """An ordinary correctness mistake (malformed SQL) is still retried,
        as opposed to a safety violation -- see the safety-violation tests
        below for the contrasting fail-closed behavior."""
        state: AgentState = {"sql": "SELEKT * FORM customers !!!", "retry_count": 0}
        result = validate_sql_node(state)

        assert result["status"] == "generating"
        assert result["retry_count"] == 1
        assert result["validation_error"] is not None
        assert result["last_error_category"] == "parse_error"
        assert result["attempt_history"][0]["outcome"] == "parse_error"
        assert result["attempt_history"][0]["will_retry"] is True

    def test_parse_error_fails_when_retries_exhausted(self):
        state: AgentState = {
            "sql": "SELEKT * FORM customers !!!",
            "retry_count": 3,
        }  # == default max_retries
        result = validate_sql_node(state)

        assert result["status"] == "failed"
        assert result["retry_count"] == 4
        assert result["failure_explanation"] is not None
        assert result["attempt_history"][0]["will_retry"] is False

    def test_safety_violation_fails_closed_immediately_with_retries_remaining(self):
        """A non-SELECT statement is a security-gate failure, not a
        correctness mistake -- it must fail closed on attempt 1 even though
        the retry budget is untouched, per CLAUDE.md's 'SQL is untrusted
        output, always.'"""
        state: AgentState = {"sql": "DROP TABLE customers", "retry_count": 0}
        result = validate_sql_node(state)

        assert result["status"] == "failed"
        assert result["last_error_category"] == "safety_violation"
        assert "retry_count" not in result  # no retry was consumed -- none was attempted
        assert result["attempt_history"][0]["outcome"] == "safety_violation"
        assert result["attempt_history"][0]["will_retry"] is False
        assert "security gate" in result["failure_explanation"]

    def test_stacked_query_is_also_a_safety_violation(self):
        state: AgentState = {"sql": "SELECT 1; DROP TABLE customers;", "retry_count": 0}
        result = validate_sql_node(state)

        assert result["status"] == "failed"
        assert result["attempt_history"][0]["outcome"] == "safety_violation"


class TestExecuteSqlNode:
    def test_success_populates_results(self, monkeypatch):
        monkeypatch.setattr(
            "agent.nodes.execute_readonly_sql",
            lambda sql, timeout, max_rows: (["id"], [(1,), (2,)]),
        )

        state: AgentState = {"sql": "SELECT id FROM customers", "retry_count": 0}
        result = execute_sql_node(state)

        assert result["status"] == "succeeded"
        assert result["row_count"] == 2
        assert result["attempt_history"][0]["outcome"] == "succeeded"

    def test_missing_reference_routes_back_to_schema_retrieval(self, monkeypatch):
        def _raise(sql, timeout, max_rows):
            raise SQLAlchemyError("(pyodbc.ProgrammingError) Invalid column name 'CustName'.")

        monkeypatch.setattr("agent.nodes.execute_readonly_sql", _raise)

        state: AgentState = {"sql": "SELECT CustName FROM customers", "retry_count": 0}
        result = execute_sql_node(state)

        assert result["status"] == "retrieving_schema"
        assert result["last_error_category"] == "missing_reference"
        assert result["attempt_history"][0]["outcome"] == "missing_reference"
        assert result["attempt_history"][0]["will_retry"] is True
        assert route_after_execution(result) == "retrieve_schema"

    def test_missing_reference_fails_when_retries_exhausted(self, monkeypatch):
        def _raise(sql, timeout, max_rows):
            raise SQLAlchemyError("Invalid object name 'Orderss'.")

        monkeypatch.setattr("agent.nodes.execute_readonly_sql", _raise)

        state: AgentState = {"sql": "SELECT * FROM Orderss", "retry_count": 3}
        result = execute_sql_node(state)

        assert result["status"] == "failed"
        assert result["failure_explanation"] is not None

    def test_syntax_error_retries_via_generate_sql(self, monkeypatch):
        def _raise(sql, timeout, max_rows):
            raise SQLAlchemyError("Incorrect syntax near 'FROM'.")

        monkeypatch.setattr("agent.nodes.execute_readonly_sql", _raise)

        state: AgentState = {"sql": "SELECT * FROM FROM customers", "retry_count": 0}
        result = execute_sql_node(state)

        assert result["status"] == "generating"
        assert result["last_error_category"] == "syntax"
        assert result["attempt_history"][0]["outcome"] == "syntax_error"
        assert route_after_execution(result) == "generate_sql"

    def test_timeout_fails_immediately_without_consuming_retry_budget_pointlessly(
        self, monkeypatch
    ):
        """A timeout should never be blindly retried, even with retries
        remaining -- retrying the same expensive query rarely helps, so the
        agent should flag it and stop rather than loop."""

        def _raise(sql, timeout, max_rows):
            raise TimeoutError("Query exceeded the 15s timeout and was aborted.")

        monkeypatch.setattr("agent.nodes.execute_readonly_sql", _raise)

        state: AgentState = {"sql": "SELECT * FROM huge_join", "retry_count": 0}
        result = execute_sql_node(state)

        assert result["status"] == "failed"
        assert result["last_error_category"] == "timeout"
        assert result["attempt_history"][0]["outcome"] == "timeout"
        assert result["attempt_history"][0]["will_retry"] is False
        assert "narrow" in result["failure_explanation"].lower()
        assert route_after_execution(result) == "failed"


class TestRoutingFunctions:
    @pytest.mark.parametrize(
        "status,expected",
        [("executing", "execute_sql"), ("failed", "failed"), ("generating", "generate_sql")],
    )
    def test_route_after_validation(self, status, expected):
        assert route_after_validation({"status": status}) == expected

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("succeeded", "succeeded"),
            ("failed", "failed"),
            ("generating", "generate_sql"),
            ("retrieving_schema", "retrieve_schema"),
        ],
    )
    def test_route_after_execution(self, status, expected):
        assert route_after_execution({"status": status}) == expected
