"""Unit tests for `GET /models` (api/main.py) -- the Ollama model-selection
registry endpoint. Fully mocked -- no real Ollama server."""

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
    ollama_allowed_models=("llama3.1:8b", "qwen2.5:7b"),
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
    log_level="INFO",
    log_redaction_level="standard",
)


def _settings(**overrides: object) -> Settings:
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.main.get_settings", lambda: _BASE_SETTINGS)
    return _BASE_SETTINGS


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class FakeOllamaClient:
    def __init__(self, models: list[str]) -> None:
        self._models = models

    def list(self) -> dict:
        return {"models": [{"model": m} for m in self._models]}


class TestGetModels:
    def test_returns_every_configured_model(self, monkeypatch, client):
        monkeypatch.setattr(
            "agent.model_registry.get_ollama_client", lambda settings: FakeOllamaClient([])
        )

        response = client.get("/models")

        assert response.status_code == 200
        body = response.json()
        assert body["provider"] == "ollama"
        assert body["default_model"] == "llama3.1:8b"
        assert body["selection_enabled"] is True
        assert {m["id"] for m in body["models"]} == {"llama3.1:8b", "qwen2.5:7b"}

    def test_marks_installed_models_from_live_ollama_discovery(self, monkeypatch, client):
        monkeypatch.setattr(
            "agent.model_registry.get_ollama_client",
            lambda settings: FakeOllamaClient(["llama3.1:8b"]),
        )

        response = client.get("/models")

        body = response.json()
        by_id = {m["id"]: m for m in body["models"]}
        assert by_id["llama3.1:8b"]["installed"] is True
        assert by_id["llama3.1:8b"]["available"] is True
        assert by_id["qwen2.5:7b"]["installed"] is False
        assert by_id["qwen2.5:7b"]["available"] is False

    def test_uninstalled_model_is_not_dropped_from_the_response(self, monkeypatch, client):
        """Requirement: 'not installed' is a status shown to the caller, not
        a reason to hide the model entirely."""
        monkeypatch.setattr(
            "agent.model_registry.get_ollama_client", lambda settings: FakeOllamaClient([])
        )

        response = client.get("/models")

        assert len(response.json()["models"]) == 2

    def test_marks_the_configured_default(self, monkeypatch, client):
        monkeypatch.setattr(
            "agent.model_registry.get_ollama_client", lambda settings: FakeOllamaClient([])
        )

        response = client.get("/models")

        by_id = {m["id"]: m for m in response.json()["models"]}
        assert by_id["llama3.1:8b"]["is_default"] is True
        assert by_id["qwen2.5:7b"]["is_default"] is False

    def test_ollama_unreachable_degrades_to_none_installed_not_a_500(self, monkeypatch, client):
        def _raise(settings):
            raise ConnectionError("refused")

        monkeypatch.setattr("agent.model_registry.get_ollama_client", _raise)

        response = client.get("/models")

        assert response.status_code == 200
        assert all(m["installed"] is False for m in response.json()["models"])

    def test_selection_disabled_reports_only_the_default(self, monkeypatch, client):
        disabled_settings = _settings(ollama_model_selection_enabled=False)
        monkeypatch.setattr("api.main.get_settings", lambda: disabled_settings)
        monkeypatch.setattr(
            "agent.model_registry.get_ollama_client", lambda settings: FakeOllamaClient([])
        )

        response = client.get("/models")

        body = response.json()
        assert body["selection_enabled"] is False
        assert [m["id"] for m in body["models"]] == ["llama3.1:8b"]

    def test_response_never_exposes_ollama_host_or_secrets(self, monkeypatch, client):
        monkeypatch.setattr(
            "agent.model_registry.get_ollama_client", lambda settings: FakeOllamaClient([])
        )

        response = client.get("/models")

        raw_body = response.text
        assert "localhost:11434" not in raw_body
        assert "secret" not in raw_body

    def test_requires_auth_when_token_configured(self, monkeypatch, client):
        auth_settings = _settings(api_auth_token=SecretStr("s3cret"))
        monkeypatch.setattr("api.main.get_settings", lambda: auth_settings)
        monkeypatch.setattr("api.auth.get_settings", lambda: auth_settings)

        response = client.get("/models")

        assert response.status_code == 401

    def test_curated_display_metadata_is_included(self, monkeypatch, client):
        monkeypatch.setattr(
            "agent.model_registry.get_ollama_client", lambda settings: FakeOllamaClient([])
        )

        response = client.get("/models")

        by_id = {m["id"]: m for m in response.json()["models"]}
        assert by_id["llama3.1:8b"]["display_name"] == "Llama 3.1 8B"
        assert "sql" in by_id["llama3.1:8b"]["capabilities"]
