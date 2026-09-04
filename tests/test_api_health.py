"""Unit tests for GET /health (api/main.py).

Fully mocked -- no real database, Ollama, or Chroma index required. Mirrors
the mocking style of tests/test_agent_nodes.py: patch the dependency at the
module that looks it up (`api.main.X`), not at its original definition site.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from config.settings import Settings
from db.connection import ConnectionTestResult
from security.secrets import SecretStr

_SETTINGS = Settings(
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


class _FakeCollection:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.main.get_settings", lambda: _SETTINGS)
    return _SETTINGS


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestHealth:
    def test_all_components_reachable_returns_200_ok(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.test_connection",
            lambda settings: ConnectionTestResult(success=True, message="Connection successful."),
        )
        monkeypatch.setattr("api.main.get_chroma_client", lambda settings: object.__new__(object))
        monkeypatch.setattr(
            "api.main.get_collection", lambda client, settings, db_name: _FakeCollection(31)
        )

        class _FakeOllamaClient:
            def __init__(self, host: str) -> None:
                self.host = host

            def list(self):
                return {"models": []}

        monkeypatch.setattr("ollama.Client", _FakeOllamaClient)

        response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["databases"][0]["name"] == "default"
        assert body["databases"][0]["connection"]["ok"] is True
        assert body["ollama"]["ok"] is True
        assert body["databases"][0]["schema_index"]["ok"] is True

    def test_database_unreachable_returns_503_degraded(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.test_connection",
            lambda settings: ConnectionTestResult(
                success=False, message="Could not reach the database host."
            ),
        )
        monkeypatch.setattr("api.main.get_chroma_client", lambda settings: object.__new__(object))
        monkeypatch.setattr(
            "api.main.get_collection", lambda client, settings, db_name: _FakeCollection(31)
        )

        class _FakeOllamaClient:
            def __init__(self, host: str) -> None:
                pass

            def list(self):
                return {"models": []}

        monkeypatch.setattr("ollama.Client", _FakeOllamaClient)

        response = client.get("/health")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["databases"][0]["connection"]["ok"] is False

    def test_empty_schema_index_is_unhealthy_not_a_crash(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.test_connection",
            lambda settings: ConnectionTestResult(success=True, message="Connection successful."),
        )
        monkeypatch.setattr("api.main.get_chroma_client", lambda settings: object.__new__(object))
        monkeypatch.setattr(
            "api.main.get_collection", lambda client, settings, db_name: _FakeCollection(0)
        )

        class _FakeOllamaClient:
            def __init__(self, host: str) -> None:
                pass

            def list(self):
                return {"models": []}

        monkeypatch.setattr("ollama.Client", _FakeOllamaClient)

        response = client.get("/health")

        assert response.status_code == 503
        assert response.json()["databases"][0]["schema_index"]["ok"] is False

    def test_ollama_unreachable_is_caught_not_raised(self, monkeypatch, client):
        """A health check must never itself crash -- an unreachable
        dependency is a reported fact (ok=False), never an unhandled
        exception bubbling out of the endpoint."""
        monkeypatch.setattr(
            "api.main.test_connection",
            lambda settings: ConnectionTestResult(success=True, message="Connection successful."),
        )
        monkeypatch.setattr("api.main.get_chroma_client", lambda settings: object.__new__(object))
        monkeypatch.setattr(
            "api.main.get_collection", lambda client, settings, db_name: _FakeCollection(31)
        )

        class _RaisingOllamaClient:
            def __init__(self, host: str) -> None:
                pass

            def list(self):
                raise ConnectionError("Connection refused")

        monkeypatch.setattr("ollama.Client", _RaisingOllamaClient)

        response = client.get("/health")

        assert response.status_code == 503
        assert response.json()["ollama"]["ok"] is False
