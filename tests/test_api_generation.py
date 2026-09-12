"""Unit tests for POST /generate/confirm (api/generation.py) -- the
human-approval confirmation step for media generation.

Fully mocked: `agent.orchestrator.nodes.execute_generation` (the only
function that actually calls IMA) is patched at the `api.generation`
module it's looked up from, mirroring `tests/test_api_execute.py`'s
patching convention. No real IMA API call is ever involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from agent.orchestrator.state import MediaGenerationResult
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
    enable_media_generation=True,
    ima_api_key=SecretStr("ima_x"),
)


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.generation.get_settings", lambda: _BASE_SETTINGS)
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


class TestConfirmGeneration:
    def test_successful_generation_returns_media_id(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.generation.execute_generation",
            lambda question, kind, settings: MediaGenerationResult(
                answer="Image generated successfully.",
                citations=[],
                status="succeeded",
                media_id="abc123",
                media_type="image",
                model="seedream-4.5",
            ),
        )

        response = client.post("/generate/confirm", json={"question": "generate a cat picture"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["media_id"] == "abc123"
        assert body["media_type"] == "image"
        assert "http" not in body["answer"]

    def test_kind_is_reinferred_server_side_not_trusted_from_client(self, monkeypatch, client):
        """GenerateConfirmRequest has no `media_type` field at all -- the
        server always re-infers it from `question` (infer_media_kind), so
        a caller can't under-report a more expensive video request as a
        cheaper image one."""
        captured: dict[str, object] = {}

        def _capture(question, kind, settings):
            captured["question"] = question
            captured["kind"] = kind
            return MediaGenerationResult(
                answer="Video generated successfully.",
                citations=[],
                status="succeeded",
                media_id="vid1",
                media_type="video",
                model="wan-2.6",
            )

        monkeypatch.setattr("api.generation.execute_generation", _capture)

        response = client.post(
            "/generate/confirm", json={"question": "animate the growth over the year"}
        )

        assert response.status_code == 200
        assert captured["kind"] == "video"

    def test_extra_fields_are_rejected(self, client):
        """GenerateConfirmRequest is extra='forbid' -- a client-supplied
        media_type (or anything else) is rejected outright, not silently
        ignored."""
        response = client.post(
            "/generate/confirm",
            json={"question": "generate a cat picture", "media_type": "image"},
        )
        assert response.status_code == 422

    def test_empty_question_is_rejected_by_request_validation(self, client):
        response = client.post("/generate/confirm", json={"question": ""})
        assert response.status_code == 422

    def test_provider_failure_surfaces_as_clean_message(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.generation.execute_generation",
            lambda question, kind, settings: MediaGenerationResult(
                answer="Image generation failed: insufficient credits",
                citations=[],
                status="failed",
                media_id=None,
                media_type="image",
                model=None,
            ),
        )

        response = client.post("/generate/confirm", json={"question": "generate a cat picture"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["media_id"] is None

    def test_rate_limit_trip_returns_429(self, monkeypatch, client):
        """SEC-08: /generate/confirm gets its own per-client-IP rate limit
        on top of the process-wide media-generation limiter -- confirm the
        per-IP one actually trips."""
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr("api.generation.get_settings", lambda: settings)
        call_count = {"n": 0}

        def _fake_execute(question, kind, settings):
            call_count["n"] += 1
            return MediaGenerationResult(
                answer="Image generated successfully.",
                citations=[],
                status="succeeded",
                media_id="abc",
                media_type="image",
                model="seedream-4.5",
            )

        monkeypatch.setattr("api.generation.execute_generation", _fake_execute)

        first = client.post("/generate/confirm", json={"question": "generate a cat picture"})
        second = client.post("/generate/confirm", json={"question": "generate a dog picture"})

        assert first.status_code == 200
        assert second.status_code == 429
        assert call_count["n"] == 1  # the second call never reached execute_generation at all
