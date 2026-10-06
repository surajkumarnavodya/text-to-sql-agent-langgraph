"""Tests for POST /analyst/investigate (Prompt 33, `api/analyst.py`).

The analysis itself is mocked at `api.analyst.run_analysis` -- its behavior is
covered by `tests/test_data_analyst_agent.py`. These tests pin the route's own
contract: the feature flag, request validation, that tenant/roles/budget come
from the server and never from the client, and the response shape.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from agent.analyst.budget import new_usage
from config.settings import Settings
from security.secrets import SecretStr

_BASE = Settings(
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
    cost_estimation_enabled=False,
    log_level="INFO",
    log_redaction_level="standard",
    enable_data_analyst_agent=True,
    analyst_max_steps=7,
    analyst_max_subqueries=3,
    analyst_max_llm_calls=1,
    analyst_max_followups=1,
    analyst_timeout_seconds=42.0,
)


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_BASE.__dict__, **overrides})


def _final_state(**overrides: object) -> dict:
    state = {
        "status": "succeeded",
        "stop_reason": None,
        "planning_mode": "planner",
        "question": "Revenue by region?",
        "subquestions": [
            {
                "id": "Q1",
                "text": "Revenue by region?",
                "origin": "planner",
                "status": "succeeded",
                "evidence_ids": ["E1"],
                "parent_evidence_id": None,
            }
        ],
        "evidence": [
            {
                "id": "E1",
                "subquestion_id": "Q1",
                "claim": "The query returned 3 row(s).",
                "truth_level": "DATABASE_FACT",
                "kind": "row_count",
                "finding_kind": None,
                "period": None,
                "sql": "SELECT 1",
                "row_count": 3,
            }
        ],
        "recommendations": [],
        "open_items": [],
        "trace": [{"seq": 1, "stage": "explain", "status": "ok", "detail": "succeeded"}],
        "usage": new_usage(time.monotonic()),
        "report_markdown": "# Analysis: Revenue by region?",
    }
    state.update(overrides)
    return state


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr("api.analyst.get_settings", lambda: _settings())
    monkeypatch.setattr("api.main.get_settings", lambda: _settings())


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def test_the_route_is_hidden_when_the_feature_flag_is_off(monkeypatch, client):
    monkeypatch.setattr(
        "api.analyst.get_settings", lambda: _settings(enable_data_analyst_agent=False)
    )

    response = client.post("/analyst/investigate", json={"question": "Revenue by region?"})

    assert response.status_code == 404


def test_an_analysis_returns_the_evidence_linked_response(monkeypatch, client):
    monkeypatch.setattr("api.analyst.run_analysis", lambda *a, **k: _final_state())

    response = client.post("/analyst/investigate", json={"question": "Revenue by region?"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["evidence"][0]["truth_level"] == "DATABASE_FACT"
    assert body["subquestions"][0]["origin"] == "planner"
    assert body["report_markdown"].startswith("# Analysis")
    assert set(body["usage"]) == {
        "steps",
        "subqueries",
        "llm_calls",
        "followups",
        "elapsed_seconds",
    }


def test_the_budget_comes_from_settings_and_never_from_the_client(monkeypatch, client):
    captured: dict = {}

    def fake(question, **kwargs):
        captured.update(kwargs)
        return _final_state()

    monkeypatch.setattr("api.analyst.run_analysis", fake)

    response = client.post(
        "/analyst/investigate",
        json={"question": "Revenue by region?"},
    )

    assert response.status_code == 200
    budget = captured["budget"]
    assert budget.max_steps == 7
    assert budget.max_subqueries == 3
    assert budget.max_llm_calls == 1
    assert budget.max_followups == 1
    assert budget.timeout_seconds == 42.0


@pytest.mark.parametrize(
    "smuggled",
    [
        {"tenant_id": "someone-elses-tenant"},
        {"caller_roles": ["admin"]},
        {"analyst_max_steps": 999},
    ],
)
def test_client_supplied_tenant_roles_or_budget_are_rejected(monkeypatch, client, smuggled):
    monkeypatch.setattr("api.analyst.run_analysis", lambda *a, **k: _final_state())

    response = client.post(
        "/analyst/investigate",
        json={"question": "Revenue by region?", **smuggled},
    )

    assert response.status_code == 422


def test_an_empty_question_is_rejected_at_the_schema_layer(client):
    response = client.post("/analyst/investigate", json={"question": ""})

    assert response.status_code == 422


def test_a_model_outside_the_allowlist_is_a_bad_request(monkeypatch, client):
    monkeypatch.setattr("api.analyst.run_analysis", lambda *a, **k: _final_state())

    response = client.post(
        "/analyst/investigate",
        json={"question": "Revenue by region?", "model": "not-an-allowed-model:latest"},
    )

    assert response.status_code == 400
