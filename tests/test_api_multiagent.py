"""Route tests for POST /analyst/supervise (Prompt 34). The supervisor itself
is covered by `tests/test_multiagent_supervisor.py`; here it is replaced at
`api.analyst.run_supervised_analysis`, so these tests pin only the route's
contract: the flag, server-side identity, and the response shape."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from agent.multiagent.contracts import AgentName, Claim
from agent.multiagent.resolution import Conflict
from agent.multiagent.supervisor import SupervisedResult
from agent.provenance import DataTruthLevel
from tests.test_api_analyst import _settings


def _result(**overrides) -> SupervisedResult:
    base = dict(
        status="succeeded",
        stop_reason=None,
        subquestions=[{"id": "Q1", "text": "Revenue by region?"}],
        claims=[
            Claim(
                agent=AgentName.SQL_DATA,
                task_id="Q1",
                text="The query returned 3 row(s).",
                truth_level=DataTruthLevel.DATABASE_FACT,
                key="row_count",
                value=3,
                grounded_in=("E1",),
            )
        ],
        conflicts=[
            Conflict(
                task_id="Q1", key="row_count", agents=("analytics", "sql_data"), outcome="withheld"
            )
        ],
        violations=["sql_data: claim cited evidence the supervisor did not issue; dropped"],
        open_items=["Conflicting values withheld."],
        trace=[
            {
                "seq": 1,
                "agent": "governance",
                "task_id": "G0",
                "status": "ok",
                "cost_units": 0,
                "detail": "",
            }
        ],
        evidence=[],
        report_markdown="# Supervised analysis",
        usage={"agent_calls": 1, "cost_units": 0, "elapsed_seconds": 0.0},
    )
    base.update(overrides)
    return SupervisedResult(**base)


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(
        "api.analyst.get_settings", lambda: _settings(enable_multi_agent_supervisor=True)
    )


def test_the_route_is_hidden_when_the_supervisor_flag_is_off(monkeypatch, client):
    monkeypatch.setattr(
        "api.analyst.get_settings", lambda: _settings(enable_multi_agent_supervisor=False)
    )

    response = client.post("/analyst/supervise", json={"question": "Revenue by region?"})

    assert response.status_code == 404


def test_a_turn_returns_claims_with_their_truth_levels_and_a_violation_count(monkeypatch, client):
    monkeypatch.setattr("api.analyst.run_supervised_analysis", lambda *a, **k: _result())

    response = client.post("/analyst/supervise", json={"question": "Revenue by region?"})

    assert response.status_code == 200
    body = response.json()
    assert body["claims"][0]["truth_level"] == "database_fact"
    assert body["claims"][0]["grounded_in"] == ["E1"]
    assert body["conflicts"][0]["outcome"] == "withheld"
    assert body["violation_count"] == 1


def test_the_response_never_echoes_rejected_content(monkeypatch, client):
    monkeypatch.setattr("api.analyst.run_supervised_analysis", lambda *a, **k: _result())

    body = client.post("/analyst/supervise", json={"question": "Revenue by region?"}).json()

    assert "did not issue" not in str(body["claims"])
    assert "violations" not in body


def test_tenant_roles_and_agent_fields_cannot_be_supplied_by_the_client(client):
    response = client.post(
        "/analyst/supervise",
        json={"question": "Revenue by region?", "tenant_id": "other", "caller_roles": ["admin"]},
    )

    assert response.status_code == 422


def test_identity_comes_from_the_server_not_the_request(monkeypatch, client):
    captured: dict = {}

    def fake(question, **kwargs):
        captured.update(kwargs)
        return _result()

    monkeypatch.setattr("api.analyst.run_supervised_analysis", fake)

    client.post("/analyst/supervise", json={"question": "Revenue by region?"})

    assert set(captured) >= {
        "caller_roles",
        "tenant_id",
        "caller_subject",
        "model",
        "conversation_history",
    }
    # No model supplied -> the validator resolves the configured default.
    assert captured["model"] == _settings().ollama_model
