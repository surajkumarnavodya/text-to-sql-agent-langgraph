"""Unit tests for POST /feedback/golden-example and POST /feedback/message
(api/main.py).

Prompt 21 (enterprise security & data governance hardening): both routes
previously had no dedicated test coverage at all, and no rate limit --
inconsistent with every other mutating route in this file (`/execute`,
`/schema/refresh`). These tests close both gaps: they exercise the
existing save behavior for the first time, and lock in the new
per-caller rate limit.
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
    """`api.rate_limit._limiters` is a process-wide singleton dict --
    reset before/after every test, same convention as
    `test_api_execute.py::_reset_api_action_limiters`."""
    import api.rate_limit as api_rate_limit

    api_rate_limit._limiters.clear()
    yield
    api_rate_limit._limiters.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestFeedbackGoldenExample:
    def test_saves_and_returns_saved_true(self, monkeypatch, client):
        captured = {}

        def _capture(question, sql, database, settings, tenant_id=None):
            captured["question"] = question
            captured["sql"] = sql
            captured["database"] = database
            captured["tenant_id"] = tenant_id

        monkeypatch.setattr("api.main.save_golden_example", _capture)

        response = client.post(
            "/feedback/golden-example",
            json={
                "question": "What were total sales last quarter?",
                "sql": "SELECT SUM(amount) FROM sales",
                "database": "default",
            },
        )

        assert response.status_code == 200
        assert response.json() == {"saved": True}
        assert captured["question"] == "What were total sales last quarter?"
        assert captured["sql"] == "SELECT SUM(amount) FROM sales"
        assert captured["database"] == "default"

    def test_rate_limit_trip_returns_429(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr("api.main.get_settings", lambda: settings)
        monkeypatch.setattr("api.main.save_golden_example", lambda *a, **k: None)
        payload = {
            "question": "q",
            "sql": "SELECT 1",
            "database": "default",
        }

        first = client.post("/feedback/golden-example", json=payload)
        second = client.post("/feedback/golden-example", json=payload)

        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers

    def test_rate_limit_is_independent_from_feedback_message(self, monkeypatch, client):
        """Each action gets its own independent budget -- exhausting
        `feedback_golden_example`'s must not affect `feedback_message`'s."""
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr("api.main.get_settings", lambda: settings)
        monkeypatch.setattr("api.main.save_golden_example", lambda *a, **k: None)
        monkeypatch.setattr("api.main.save_response_feedback", lambda *a, **k: None)

        golden_payload = {"question": "q", "sql": "SELECT 1", "database": "default"}
        message_payload = {"question": "q", "answer": "a", "rating": "positive"}

        exhaust = client.post("/feedback/golden-example", json=golden_payload)
        tripped = client.post("/feedback/golden-example", json=golden_payload)
        still_ok = client.post("/feedback/message", json=message_payload)

        assert exhaust.status_code == 200
        assert tripped.status_code == 429
        assert still_ok.status_code == 200


class TestFeedbackMessage:
    def test_saves_and_returns_saved_true(self, monkeypatch, client):
        captured = {}

        def _capture(question, answer, rating, **kwargs):
            captured["question"] = question
            captured["answer"] = answer
            captured["rating"] = rating

        monkeypatch.setattr("api.main.save_response_feedback", _capture)

        response = client.post(
            "/feedback/message",
            json={
                "question": "What were total sales last quarter?",
                "answer": "Total sales were $1,000.",
                "rating": "positive",
            },
        )

        assert response.status_code == 200
        assert response.json() == {"saved": True}
        assert captured["rating"] == "positive"

    def test_rate_limit_trip_returns_429(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr("api.main.get_settings", lambda: settings)
        monkeypatch.setattr("api.main.save_response_feedback", lambda *a, **k: None)
        payload = {"question": "q", "answer": "a", "rating": "negative"}

        first = client.post("/feedback/message", json=payload)
        second = client.post("/feedback/message", json=payload)

        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers
