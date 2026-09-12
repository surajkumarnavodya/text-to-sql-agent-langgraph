"""Unit tests for POST /execute (api/main.py) -- the API equivalent of
`ui/app.py`'s "Confirm and Run" button.

Fully mocked, same style as `tests/test_api_ask.py`: `execute_readonly_sql`
and `get_read_only_engine` are patched at the `api.main` module they're
looked up from. `validate_sql`/`enforce_row_limit`/`qualify_table_schema`
are the real functions (not mocked) so this also exercises the actual SQL
allowlist against real SQL text, exactly like `ui/app.py`'s own flow does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

import api.main as api_main
from config.settings import Settings
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


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.main.get_settings", lambda: _BASE_SETTINGS)
    monkeypatch.setattr("api.main.get_read_only_engine", lambda config: object())
    return _BASE_SETTINGS


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestExecute:
    def test_valid_select_is_executed_and_returns_a_chart(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (
                ["region", "revenue"],
                [("East", 100), ("West", 200)],
            ),
        )

        response = client.post(
            "/execute", json={"sql": "SELECT region, revenue FROM sales", "database": "default"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["database"] == "default"
        assert body["result_columns"] == ["region", "revenue"]
        assert body["row_count"] == 2
        assert body["duration_ms"] is not None
        # Two categories + one numeric column -> the auto-pick heuristic
        # (agent.result_charting.build_chart) should produce a bar chart.
        assert body["chart"] is not None
        assert body["chart"]["data"][0]["type"] == "bar"

    def test_no_chart_for_an_all_numeric_result(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (["a", "b"], [(1, 2)]),
        )

        response = client.post("/execute", json={"sql": "SELECT a, b FROM t"})

        assert response.status_code == 200
        assert response.json()["chart"] is None

    def test_destructive_sql_is_rejected_not_executed(self, monkeypatch, client):
        called = False

        def _fail(*a, **k):
            nonlocal called
            called = True
            raise AssertionError("must not execute a rejected statement")

        monkeypatch.setattr("api.main.execute_readonly_sql", _fail)

        response = client.post("/execute", json={"sql": "DROP TABLE users"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "rejected"
        assert body["error"].startswith("Rejected:")
        assert not called

    def test_execution_failure_is_redacted_not_raw(self, monkeypatch, client):
        def _raise(sql, timeout, max_rows, engine=None):
            raise SQLAlchemyError(
                "connection to server failed: password authentication failed for user "
                "'reader' password='hunter2'"
            )

        monkeypatch.setattr("api.main.execute_readonly_sql", _raise)

        response = client.post("/execute", json={"sql": "SELECT 1"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert "hunter2" not in body["error"]

    def test_real_sqlalchemy_row_objects_serialize_correctly(self, monkeypatch, client):
        """Regression guard: `execute_readonly_sql` returns
        `sqlalchemy.engine.row.Row` objects (via `CursorResult.fetchmany`),
        not plain tuples -- `Row` isn't recognized by `fastapi.encoders
        .jsonable_encoder`'s isinstance checks and previously raised
        ValueError('object is not iterable', ...) the first time this
        endpoint ran against a real database. Uses a real (in-memory
        SQLite) engine, not a mock, so this actually exercises real `Row`
        objects rather than a mock that happens to look like one."""
        engine = create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            real_columns = list(conn.execute(text("SELECT 1 AS a, 'x' AS b")).keys())
            real_rows = conn.execute(text("SELECT 1 AS a, 'x' AS b")).fetchall()
        assert type(real_rows[0]).__name__ == "Row"

        monkeypatch.setattr(
            "api.main.execute_readonly_sql",
            lambda sql, timeout, max_rows, engine=None: (real_columns, real_rows),
        )

        response = client.post("/execute", json={"sql": "SELECT 1 AS a, 'x' AS b"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["result_rows"] == [[1, "x"]]

    def test_unknown_database_returns_404(self, client):
        response = client.post("/execute", json={"sql": "SELECT 1", "database": "nope"})
        assert response.status_code == 404

    def test_empty_sql_is_rejected_by_request_validation(self, client):
        response = client.post("/execute", json={"sql": ""})
        assert response.status_code == 422
