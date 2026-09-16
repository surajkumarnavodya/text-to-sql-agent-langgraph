"""Unit tests for voice/correction.py -- LLM-based transcript cleanup.

Ollama is always mocked (`agent.llm_client.get_ollama_client` patched at the
module it's looked up from) -- no real model call in the pytest suite,
matching `tests/test_voice_stt.py`'s "backend always mocked" convention.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from config.settings import Settings
from security.secrets import SecretStr
from voice.correction import correct_transcript

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
    enable_voice_correction=True,
    voice_correction_max_tokens=150,
)


class _FakeOllamaClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.last_call = None

    def chat(self, **kwargs):
        self.last_call = kwargs
        if self._exc is not None:
            raise self._exc
        return self._response


class TestCorrectTranscript:
    def test_happy_path_returns_corrected_text(self, monkeypatch):
        fake_client = _FakeOllamaClient(
            response={"message": {"content": "How many employees are in the Sales department?"}}
        )
        monkeypatch.setattr("voice.correction.get_ollama_client", lambda settings: fake_client)

        result = correct_transcript(
            "um how many employes uh in the sales dept", settings=_BASE_SETTINGS
        )

        assert result == "How many employees are in the Sales department?"
        assert fake_client.last_call["model"] == "llama3.1:8b"
        assert fake_client.last_call["messages"][1]["content"] == (
            "um how many employes uh in the sales dept"
        )

    def test_disabled_returns_raw_text_without_calling_ollama(self, monkeypatch):
        def _fail_if_called(settings):
            raise AssertionError("must not call Ollama when correction is disabled")

        monkeypatch.setattr("voice.correction.get_ollama_client", _fail_if_called)
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "enable_voice_correction": False})

        result = correct_transcript("um how many employes", settings=settings)

        assert result == "um how many employes"

    def test_blank_text_returns_unchanged_without_calling_ollama(self, monkeypatch):
        def _fail_if_called(settings):
            raise AssertionError("must not call Ollama for blank input")

        monkeypatch.setattr("voice.correction.get_ollama_client", _fail_if_called)

        result = correct_transcript("   ", settings=_BASE_SETTINGS)

        assert result == "   "

    def test_unreachable_ollama_fails_open_to_raw_text(self, monkeypatch):
        fake_client = _FakeOllamaClient(exc=httpx.ConnectError("connection refused"))
        monkeypatch.setattr("voice.correction.get_ollama_client", lambda settings: fake_client)

        result = correct_transcript("um how many employes", settings=_BASE_SETTINGS)

        assert result == "um how many employes"

    def test_empty_model_response_falls_back_to_raw_text(self, monkeypatch):
        fake_client = _FakeOllamaClient(response={"message": {"content": "   "}})
        monkeypatch.setattr("voice.correction.get_ollama_client", lambda settings: fake_client)

        result = correct_transcript("um how many employes", settings=_BASE_SETTINGS)

        assert result == "um how many employes"

    def test_object_shaped_response_is_read_via_attribute_access(self, monkeypatch):
        class _Message:
            content = "Show the employee list."

        class _Response:
            message = _Message()

        fake_client = _FakeOllamaClient(response=_Response())
        monkeypatch.setattr("voice.correction.get_ollama_client", lambda settings: fake_client)

        result = correct_transcript("show me employ list", settings=_BASE_SETTINGS)

        assert result == "Show the employee list."
