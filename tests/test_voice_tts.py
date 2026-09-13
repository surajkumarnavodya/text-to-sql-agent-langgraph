"""Unit tests for voice/tts.py -- text-to-speech.

The Piper voice itself is always mocked (`_get_piper_voice` patched at the
module it's looked up from) -- no real voice model file/inference in the
pytest suite.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from config.settings import Settings
from security.secrets import SecretStr
from voice.exceptions import VoiceModelNotFoundError, VoiceProcessingError
from voice.tts import synthesize

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
    tts_voice="en_US-lessac-medium",
)


class _FakeVoice:
    def synthesize_wav(self, text: str, wav_file: wave.Wave_write, **_kwargs) -> None:
        wav_file.setframerate(22050)
        wav_file.setsampwidth(2)
        wav_file.setnchannels(1)
        wav_file.writeframes(b"\x00\x00" * 10)


class TestSynthesize:
    def test_happy_path_returns_wav_bytes_and_duration(self, monkeypatch):
        monkeypatch.setattr(
            "voice.tts._get_piper_voice", lambda voice_name, model_path: _FakeVoice()
        )

        result = synthesize("Found twelve rows.", settings=_BASE_SETTINGS)

        assert result.audio_bytes.startswith(b"RIFF")
        assert result.tts_duration_ms >= 0

    def test_missing_model_file_raises_voice_model_not_found_error(self, monkeypatch):
        def _raise(voice_name, model_path):
            raise VoiceModelNotFoundError("no model file")

        monkeypatch.setattr("voice.tts._get_piper_voice", _raise)

        with pytest.raises(VoiceModelNotFoundError):
            synthesize("hello", settings=_BASE_SETTINGS)

    def test_synthesis_failure_is_wrapped_as_voice_processing_error(self, monkeypatch):
        class _BrokenVoice:
            def synthesize_wav(self, *_args, **_kwargs):
                raise RuntimeError("synthesis exploded")

        monkeypatch.setattr(
            "voice.tts._get_piper_voice", lambda voice_name, model_path: _BrokenVoice()
        )

        with pytest.raises(VoiceProcessingError):
            synthesize("hello", settings=_BASE_SETTINGS)
