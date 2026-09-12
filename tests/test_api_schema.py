"""Unit tests for POST /schema/refresh (api/main.py).

Minimal, focused on SEC-08 (this route previously had no rate limit of its
own at all) -- not a full backfill of pre-existing coverage for this route,
which had none before this pass either.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
    return _BASE_SETTINGS


@pytest.fixture(autouse=True)
def _reset_api_action_limiters():
    import api.rate_limit as api_rate_limit

    api_rate_limit._limiters.clear()
    yield
    api_rate_limit._limiters.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestSchemaRefresh:
    def test_returns_table_counts_per_database(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.main.refresh_all_schema_indexes",
            lambda settings: {"default": [object(), object()]},
        )

        response = client.post("/schema/refresh")

        assert response.status_code == 200
        body = response.json()
        assert body["databases"] == [{"database": "default", "table_count": 2}]

    def test_rate_limit_trip_returns_429(self, monkeypatch, client):
        """SEC-08: /schema/refresh previously had no rate limit at all,
        despite re-introspecting/re-embedding every configured database
        being real, non-trivial work."""
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr("api.main.get_settings", lambda: settings)
        monkeypatch.setattr("api.main.refresh_all_schema_indexes", lambda settings: {})

        first = client.post("/schema/refresh")
        second = client.post("/schema/refresh")

        assert first.status_code == 200
        assert second.status_code == 429
