"""Unit tests for POST /voice/transcribe and POST /voice/synthesize
(api/voice.py). Fully mocked: `voice.stt.transcribe`/`voice.tts.synthesize`
are patched at the `api.voice` module they're looked up from -- no real
model ever loaded in the pytest suite. Mirrors `tests/test_api_media.py`/
`tests/test_api_generation.py`'s structure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from config.settings import Settings
from security.secrets import SecretStr
from voice.exceptions import VoiceInputTooLongError, VoiceModelNotFoundError, VoiceProcessingError
from voice.stt import TranscriptionResult
from voice.tts import SynthesisResult

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
    enable_voice_mode=True,
    voice_max_upload_mb=1,
    voice_max_duration_seconds=30,
)


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.voice.get_settings", lambda: _BASE_SETTINGS)
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


class TestVoiceModeDisabled:
    def test_transcribe_404s_when_disabled(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "enable_voice_mode": False})
        monkeypatch.setattr("api.voice.get_settings", lambda: settings)

        response = client.post(
            "/voice/transcribe", files={"audio": ("q.webm", b"fake", "audio/webm")}
        )

        assert response.status_code == 404

    def test_synthesize_404s_when_disabled(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "enable_voice_mode": False})
        monkeypatch.setattr("api.voice.get_settings", lambda: settings)

        response = client.post("/voice/synthesize", json={"text": "hello"})

        assert response.status_code == 404


class TestTranscribeAudio:
    def test_happy_path_returns_text_and_duration(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.voice.transcribe",
            lambda audio_bytes, settings: TranscriptionResult(
                text="how many customers are there", stt_duration_ms=42.0
            ),
        )

        response = client.post(
            "/voice/transcribe", files={"audio": ("q.webm", b"fake audio", "audio/webm")}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["text"] == "how many customers are there"
        assert body["stt_duration_ms"] == 42.0

    def test_oversized_upload_is_rejected_before_transcription(self, monkeypatch, client):
        model_was_called = {"value": False}

        def _fail_if_called(audio_bytes, settings):
            model_was_called["value"] = True
            raise AssertionError("must not transcribe an oversized upload")

        monkeypatch.setattr("api.voice.transcribe", _fail_if_called)
        oversized = b"x" * (2 * 1024 * 1024)  # 2MB > 1MB cap

        response = client.post(
            "/voice/transcribe", files={"audio": ("q.webm", oversized, "audio/webm")}
        )

        assert response.status_code == 413
        assert model_was_called["value"] is False

    def test_too_long_recording_surfaces_as_413(self, monkeypatch, client):
        def _raise(audio_bytes, settings):
            raise VoiceInputTooLongError("too long")

        monkeypatch.setattr("api.voice.transcribe", _raise)

        response = client.post(
            "/voice/transcribe", files={"audio": ("q.webm", b"fake", "audio/webm")}
        )

        assert response.status_code == 413

    def test_processing_failure_surfaces_as_clean_500(self, monkeypatch, client):
        def _raise(audio_bytes, settings):
            raise VoiceProcessingError(
                "boom", safe_message="Voice mode is temporarily unavailable."
            )

        monkeypatch.setattr("api.voice.transcribe", _raise)

        response = client.post(
            "/voice/transcribe", files={"audio": ("q.webm", b"fake", "audio/webm")}
        )

        assert response.status_code == 500
        assert "boom" not in response.json()["detail"]

    def test_rate_limit_trip_returns_429(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr("api.voice.get_settings", lambda: settings)
        monkeypatch.setattr(
            "api.voice.transcribe",
            lambda audio_bytes, settings: TranscriptionResult(text="hi", stt_duration_ms=1.0),
        )

        first = client.post("/voice/transcribe", files={"audio": ("q.webm", b"a", "audio/webm")})
        second = client.post("/voice/transcribe", files={"audio": ("q.webm", b"b", "audio/webm")})

        assert first.status_code == 200
        assert second.status_code == 429


class TestSynthesizeSpeech:
    def test_happy_path_returns_wav_bytes(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.voice.synthesize",
            lambda text, settings: SynthesisResult(
                audio_bytes=b"RIFF....WAVEfmt ", tts_duration_ms=12.0
            ),
        )

        response = client.post("/voice/synthesize", json={"text": "Found twelve rows."})

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content == b"RIFF....WAVEfmt "

    def test_text_over_max_question_length_is_rejected(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "max_question_length": 10})
        monkeypatch.setattr("api.voice.get_settings", lambda: settings)
        model_was_called = {"value": False}

        def _fail_if_called(text, settings):
            model_was_called["value"] = True
            raise AssertionError("must not synthesize over-length text")

        monkeypatch.setattr("api.voice.synthesize", _fail_if_called)

        response = client.post(
            "/voice/synthesize", json={"text": "this text is definitely too long"}
        )

        assert response.status_code == 413
        assert model_was_called["value"] is False

    def test_missing_voice_model_surfaces_as_503(self, monkeypatch, client):
        def _raise(text, settings):
            raise VoiceModelNotFoundError("no model", safe_message="Voice model not downloaded.")

        monkeypatch.setattr("api.voice.synthesize", _raise)

        response = client.post("/voice/synthesize", json={"text": "hello"})

        assert response.status_code == 503

    def test_empty_text_is_rejected_by_request_validation(self, client):
        response = client.post("/voice/synthesize", json={"text": ""})
        assert response.status_code == 422

    def test_rate_limit_trip_returns_429(self, monkeypatch, client):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "api_action_rate_limit_per_minute": 1})
        monkeypatch.setattr("api.voice.get_settings", lambda: settings)
        monkeypatch.setattr(
            "api.voice.synthesize",
            lambda text, settings: SynthesisResult(audio_bytes=b"RIFF", tts_duration_ms=1.0),
        )

        first = client.post("/voice/synthesize", json={"text": "hello"})
        second = client.post("/voice/synthesize", json={"text": "world"})

        assert first.status_code == 200
        assert second.status_code == 429
