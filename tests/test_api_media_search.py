"""Unit tests for POST /search/media (api/media_search.py) -- direct media
search, independent of the conversational /ask flow. `media.search
.search_media` is mocked at `api.media_search` (where it's looked up from)
-- no real embedding model ever loaded in the pytest suite. Mirrors
`tests/test_api_voice.py`'s structure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from config.settings import Settings
from media.search import MediaHit
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
    enable_media_search=True,
    media_library_path=Path("/tmp/media-library"),
)


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.media_search.get_settings", lambda: _BASE_SETTINGS)
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


class TestMediaSearchDisabled:
    def test_404s_when_disabled(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "enable_media_search": False})
        monkeypatch.setattr("api.media_search.get_settings", lambda: settings)

        response = client.post("/search/media", json={"query": "a cat"})

        assert response.status_code == 404


class TestSearchMediaEndpoint:
    def test_returns_hits_and_a_templated_summary(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.media_search.search_media",
            lambda query, settings, media_type: [
                MediaHit(media_id="img1", media_type="image", caption="a photo", similarity=0.9)
            ],
        )

        response = client.post("/search/media", json={"query": "the site inspection photo"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert "1 result" in body["answer"]
        assert body["hits"] == [
            {
                "media_id": "img1",
                "media_type": "image",
                "caption": "a photo",
                "timestamp_start": None,
                "timestamp_end": None,
            }
        ]

    def test_no_hits_returns_insufficient_information(self, monkeypatch, client):
        monkeypatch.setattr("api.media_search.search_media", lambda query, settings, media_type: [])

        response = client.post("/search/media", json={"query": "something that doesn't exist"})

        assert response.status_code == 200
        assert response.json()["status"] == "insufficient_information"
        assert response.json()["hits"] == []

    def test_rejects_an_empty_query(self, client):
        response = client.post("/search/media", json={"query": ""})
        assert response.status_code == 422

    def test_rejects_unknown_fields(self, client):
        response = client.post("/search/media", json={"query": "x", "extra_field": "nope"})
        assert response.status_code == 422
