"""Unit tests for POST /ask and GET /schema/tables (api/main.py).

Fully mocked -- `agent.orchestrator.graph.run_orchestrated` (what `api.main`
actually calls -- a pass-through to `agent.graph.run_agent` when
`ENABLE_MULTI_SOURCE_ROUTER` is unset, its default in these tests) and the
DB/Chroma calls are patched at the `api.main` module they're looked up from,
exactly like `tests/test_agent_nodes.py` mocks `agent.nodes`'s own
dependencies. No real LLM call, database connection, or Chroma index is ever
touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import api.main as api_main
from agent.exceptions import SchemaRetrievalError
from agent.orchestrator.state import OrchestratorState
from agent.state import AgentState
from config.settings import Settings
from db.schema_introspection import ColumnInfo, TableSchemaInfo
from security.secrets import SecretStr

_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="postgresql",
    db_host="db.example.com",
    db_port=5432,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("secret"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="x",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="schema_ddl",
    embedding_model_name="all-MiniLM-L6-v2",
    schema_top_k=4,
    max_retries=3,
    complex_query_max_retry_bonus=2,
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


def _settings(**overrides: object) -> Settings:
    """See `tests/test_connection.py::_settings` for why this rebuilds via
    `Settings(**{**_BASE_SETTINGS.__dict__, **overrides})` rather than
    `BaseModel.model_copy(update=...)`."""
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.main.get_settings", lambda: _BASE_SETTINGS)
    return _BASE_SETTINGS


@pytest.fixture(autouse=True)
def _reset_ip_limiters():
    """`api.main._ip_limiters` is a process-wide singleton dict by design
    (mirrors `agent.rate_limit.get_llm_call_limiter`'s own module-level
    singleton, per that module's docstring) -- reset before every test so
    one test's requests can't trip another's rate limit purely by test
    order/count."""
    api_main._ip_limiters.clear()
    yield
    api_main._ip_limiters.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestAsk:
    def test_successful_answer_is_returned(self, monkeypatch, client):
        final_state: AgentState = {
            "status": "succeeded",
            "sql": "SELECT COUNT(*) FROM t",
            "result_columns": ["cnt"],
            "result_rows": [(5,)],
            "row_count": 1,
            "retry_count": 0,
            "attempt_history": [
                {
                    "attempt": 1,
                    "sql": "SELECT COUNT(*) FROM t",
                    "outcome": "succeeded",
                    "error": None,
                    "will_retry": False,
                }
            ],
            "insight": "There are 5 rows.",
            "error_history": [],
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        response = client.post("/ask", json={"question": "How many rows are there?"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["sql"] == "SELECT COUNT(*) FROM t"
        assert body["result_rows"] == [[5]]
        assert body["attempt_history"][0]["outcome"] == "succeeded"
        assert "X-Correlation-ID" in response.headers

    def test_conversation_history_is_forwarded_to_run_agent(self, monkeypatch, client):
        captured = {}

        def _capture(question, conversation_history=None, enable_insight=True, session_id=None):
            captured["question"] = question
            captured["conversation_history"] = conversation_history
            captured["enable_insight"] = enable_insight
            captured["session_id"] = session_id
            return {"status": "succeeded", "error_history": []}

        monkeypatch.setattr("api.main.run_orchestrated", _capture)

        response = client.post(
            "/ask",
            json={
                "question": "And last year?",
                "conversation_history": [
                    {
                        "question": "Total sales in 2012?",
                        "sql": "SELECT SUM(x) FROM t",
                        "tables": ["t"],
                        "status": "succeeded",
                    }
                ],
                "enable_insight": False,
            },
        )

        assert response.status_code == 200
        assert captured["question"] == "And last year?"
        assert captured["conversation_history"][0]["question"] == "Total sales in 2012?"
        assert captured["enable_insight"] is False

    def test_schema_retrieval_error_becomes_a_failed_status_not_a_500(self, monkeypatch, client):
        def _raise(*a, **k):
            raise SchemaRetrievalError("Chroma index is empty -- run scripts/build_embeddings.py.")

        monkeypatch.setattr("api.main.run_orchestrated", _raise)

        response = client.post("/ask", json={"question": "How many rows are there?"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        # The raw internal detail ("Chroma index is empty -- run scripts/
        # build_embeddings.py.") must NOT reach the response body -- only
        # SchemaRetrievalError.safe_message may. See agent/exceptions.py's
        # module docstring and api/main.py's /ask handler.
        assert body["error_history"][0] == SchemaRetrievalError("x").safe_message
        assert "Chroma index is empty" not in body["error_history"][0]

    def test_empty_question_is_rejected_by_request_validation(self, client):
        response = client.post("/ask", json={"question": ""})
        assert response.status_code == 422

    def test_rate_limit_trip_returns_429(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.get_settings", lambda: _settings(question_rate_limit_per_minute=1)
        )
        monkeypatch.setattr(
            "api.main.run_orchestrated",
            lambda *a, **k: {"status": "succeeded", "error_history": []},
        )

        first = client.post("/ask", json={"question": "q1"})
        second = client.post("/ask", json={"question": "q2"})

        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers

    def test_multi_source_fields_are_surfaced(self, monkeypatch, client):
        """Regression guard for the gap the pre-React-migration API audit
        found: sources_used/citations/synthesized_answer/schema_tables/
        query_plan/followup fields must reach the client, not just the
        SQL-only subset AskResponse originally covered."""
        final_state: OrchestratorState = {
            "status": "succeeded",
            "sql": "SELECT 1",
            "error_history": [],
            "sources_used": ["sql", "policy"],
            "synthesized_answer": "**Database**: 1 row(s) returned.\n\n**Policy**: Leave policy is 20 days.",
            "policy_result": {
                "answer": "Leave policy is 20 days.",
                "citations": [
                    {
                        "filename": "leave.pdf",
                        "chunk_index": 0,
                        "page_number": 1,
                        "document_id": "doc-1",
                        "has_pdf_bytes": True,
                    }
                ],
                "status": "succeeded",
            },
            "query_plan": ["Group by region", "Sum revenue"],
            "schema_tables": [
                {"table_name": "Sales", "ddl": "CREATE TABLE Sales (...)", "similarity_score": 0.9}
            ],
            "followup_classification": "followup",
            "followup_resolved_against": {
                "question": "Total sales in 2012?",
                "sql": "SELECT SUM(x) FROM t",
                "tables": ["t"],
                "status": "succeeded",
            },
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        response = client.post("/ask", json={"question": "And the leave policy?"})

        assert response.status_code == 200
        body = response.json()
        assert body["sources_used"] == ["sql", "policy"]
        assert "Leave policy" in body["synthesized_answer"]
        assert body["policy_result"]["citations"][0]["document_id"] == "doc-1"
        assert body["policy_result"]["citations"][0]["has_pdf_bytes"] is True
        assert body["query_plan"] == ["Group by region", "Sum revenue"]
        assert body["schema_tables"][0]["table_name"] == "Sales"
        assert body["followup_classification"] == "followup"
        assert body["followup_resolved_against"]["question"] == "Total sales in 2012?"

    def test_real_sqlalchemy_row_objects_in_result_rows_serialize_correctly(
        self, monkeypatch, client
    ):
        """Regression guard: `agent.nodes.execute_sql_node` populates
        `result_rows` from `db.execution.execute_readonly_sql`, which
        returns `sqlalchemy.engine.row.Row` objects, not plain tuples --
        `Row` isn't recognized by `fastapi.encoders.jsonable_encoder`'s
        isinstance checks and previously raised ValueError('object is not
        iterable', ...) the first time /ask ran against a real database
        (mocked-`run_orchestrated` tests never exercised a real Row)."""
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            real_rows = conn.execute(text("SELECT 1 AS cnt")).fetchall()
        assert type(real_rows[0]).__name__ == "Row"

        final_state: AgentState = {
            "status": "succeeded",
            "sql": "SELECT COUNT(*) AS cnt FROM t",
            "result_columns": ["cnt"],
            # Real Row objects, not tuples -- see docstring above.
            "result_rows": real_rows,  # type: ignore[typeddict-item]
            "row_count": 1,
            "error_history": [],
        }
        monkeypatch.setattr("api.main.run_orchestrated", lambda *a, **k: final_state)

        response = client.post("/ask", json={"question": "How many rows?"})

        assert response.status_code == 200
        assert response.json()["result_rows"] == [[1]]

    def test_auth_required_when_token_configured(self, monkeypatch, client):
        auth_settings = _settings(api_auth_token=SecretStr("s3cret"))
        monkeypatch.setattr("api.main.get_settings", lambda: auth_settings)
        # verify_api_key (api/auth.py) reads settings via its own imported
        # get_settings, not api.main's -- both must be patched.
        monkeypatch.setattr("api.auth.get_settings", lambda: auth_settings)
        monkeypatch.setattr(
            "api.main.run_orchestrated",
            lambda *a, **k: {"status": "succeeded", "error_history": []},
        )

        no_header = client.post("/ask", json={"question": "q"})
        wrong_token = client.post(
            "/ask", json={"question": "q"}, headers={"Authorization": "Bearer wrong"}
        )
        right_token = client.post(
            "/ask", json={"question": "q"}, headers={"Authorization": "Bearer s3cret"}
        )

        assert no_header.status_code == 401
        assert wrong_token.status_code == 401
        assert right_token.status_code == 200


class TestSchemaTables:
    def test_returns_introspected_tables(self, monkeypatch, client):
        tables = [
            TableSchemaInfo(
                table_name="DimCustomer",
                columns=(
                    ColumnInfo(name="CustomerKey", type="INT", nullable=False, is_primary_key=True),
                    ColumnInfo(
                        name="EmailAddress",
                        type="NVARCHAR(50)",
                        nullable=True,
                        is_primary_key=False,
                    ),
                ),
                foreign_keys=(),
                ddl="CREATE TABLE DimCustomer (...)",
            )
        ]
        monkeypatch.setattr("api.main.get_read_only_engine", lambda settings: object())
        monkeypatch.setattr("api.main.introspect_schema", lambda engine, schema: tables)

        response = client.get("/schema/tables")

        assert response.status_code == 200
        body = response.json()
        assert len(body["tables"]) == 1
        assert body["tables"][0]["table_name"] == "DimCustomer"
        assert body["tables"][0]["columns"][0]["is_primary_key"] is True

    def test_requires_auth_when_token_configured(self, monkeypatch, client):
        auth_settings = _settings(api_auth_token=SecretStr("s3cret"))
        monkeypatch.setattr("api.main.get_settings", lambda: auth_settings)
        monkeypatch.setattr("api.auth.get_settings", lambda: auth_settings)
        monkeypatch.setattr("api.main.get_read_only_engine", lambda settings: object())
        monkeypatch.setattr("api.main.introspect_schema", lambda engine, schema: [])

        response = client.get("/schema/tables")

        assert response.status_code == 401
