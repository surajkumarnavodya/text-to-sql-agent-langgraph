"""Unit tests for voice/stt.py -- speech-to-text.

The Whisper model itself is always mocked (`_get_whisper_model` patched at
the module it's looked up from) -- no real model download/inference in the
pytest suite. Duration probing uses a real, tiny, deterministic in-memory
WAV file (no network, no external fixture) so that part of the pipeline is
exercised for real, matching this project's "fully mocked but not
fake-shaped" testing style elsewhere (e.g. `tests/test_schema_introspection.py`).
"""

from __future__ import annotations

import io
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

import pytest

from config.settings import Settings
from db.schema_introspection import ColumnInfo, TableSchemaInfo
from security.secrets import SecretStr
from voice.exceptions import VoiceInputTooLongError, VoiceProcessingError
from voice.stt import _build_vocabulary_hint, transcribe

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
    voice_max_duration_seconds=30,
    stt_vocabulary_max_chars=200,
)


def _silent_wav_bytes(seconds: float, sample_rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    frame_count = int(seconds * sample_rate)
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(struct.pack(f"<{frame_count}h", *([0] * frame_count)))
    return buffer.getvalue()


@dataclass
class _FakeSegment:
    text: str


class _FakeWhisperModel:
    def __init__(self, segments_text: list[str]):
        self._segments_text = segments_text

    def transcribe(self, _audio, **_kwargs):
        return ([_FakeSegment(text=t) for t in self._segments_text], object())


class TestTranscribe:
    def test_happy_path_returns_joined_text_and_duration(self, monkeypatch):
        monkeypatch.setattr(
            "voice.stt._get_whisper_model",
            lambda model_size, device: _FakeWhisperModel(["how many", "customers are there"]),
        )
        monkeypatch.setattr("voice.stt._build_vocabulary_hint", lambda settings: "")

        result = transcribe(_silent_wav_bytes(1.0), settings=_BASE_SETTINGS)

        assert result.text == "how many customers are there"
        assert result.stt_duration_ms >= 0

    def test_over_duration_recording_is_rejected_before_running_the_model(self, monkeypatch):
        model_was_called = {"value": False}

        def _fail_if_called(model_size, device):
            model_was_called["value"] = True
            raise AssertionError("Whisper model must not be loaded for an over-long recording")

        monkeypatch.setattr("voice.stt._get_whisper_model", _fail_if_called)
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "voice_max_duration_seconds": 1})

        with pytest.raises(VoiceInputTooLongError):
            transcribe(_silent_wav_bytes(2.0), settings=settings)

        assert model_was_called["value"] is False

    def test_undecodable_audio_raises_voice_processing_error(self):
        with pytest.raises(VoiceProcessingError):
            transcribe(b"not a real audio file", settings=_BASE_SETTINGS)

    def test_model_failure_is_wrapped_as_voice_processing_error(self, monkeypatch):
        class _BrokenModel:
            def transcribe(self, _audio, **_kwargs):
                raise RuntimeError("model exploded")

        monkeypatch.setattr(
            "voice.stt._get_whisper_model", lambda model_size, device: _BrokenModel()
        )
        monkeypatch.setattr("voice.stt._build_vocabulary_hint", lambda settings: "")

        with pytest.raises(VoiceProcessingError):
            transcribe(_silent_wav_bytes(0.5), settings=_BASE_SETTINGS)


class TestBuildVocabularyHint:
    def test_collects_deduped_table_and_column_names_across_databases(self, monkeypatch):
        table = TableSchemaInfo(
            table_name="orders",
            columns=(
                ColumnInfo(name="order_id", type="INT", nullable=False, is_primary_key=True),
                ColumnInfo(name="branch_id", type="INT", nullable=False, is_primary_key=False),
            ),
            foreign_keys=(),
            ddl="CREATE TABLE orders (...)",
        )
        monkeypatch.setattr("voice.stt.list_connection_names", lambda settings: ["default"])
        monkeypatch.setattr("voice.stt.get_connection", lambda settings, name: settings)
        monkeypatch.setattr("voice.stt.get_read_only_engine", lambda connection: object())
        monkeypatch.setattr("voice.stt.introspect_schema", lambda engine, schema: [table])

        hint = _build_vocabulary_hint(_BASE_SETTINGS)

        assert "orders" in hint
        assert "branch_id" in hint

    def test_fails_open_to_empty_string_on_introspection_error(self, monkeypatch):
        def _raise(settings):
            raise RuntimeError("no database configured")

        monkeypatch.setattr("voice.stt.list_connection_names", _raise)

        assert _build_vocabulary_hint(_BASE_SETTINGS) == ""

    def test_hint_never_exceeds_configured_max_length(self, monkeypatch):
        many_columns = tuple(
            ColumnInfo(
                name=f"some_very_long_column_name_{i}",
                type="INT",
                nullable=False,
                is_primary_key=False,
            )
            for i in range(50)
        )
        table = TableSchemaInfo(
            table_name="wide_table", columns=many_columns, foreign_keys=(), ddl=""
        )
        monkeypatch.setattr("voice.stt.list_connection_names", lambda settings: ["default"])
        monkeypatch.setattr("voice.stt.get_connection", lambda settings, name: settings)
        monkeypatch.setattr("voice.stt.get_read_only_engine", lambda connection: object())
        monkeypatch.setattr("voice.stt.introspect_schema", lambda engine, schema: [table])
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "stt_vocabulary_max_chars": 50})

        hint = _build_vocabulary_hint(settings)

        assert len(hint) <= 50
