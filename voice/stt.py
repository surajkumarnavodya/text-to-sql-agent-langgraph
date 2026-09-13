"""Speech-to-text: wraps `faster-whisper` behind a single `transcribe()`
call, so the backend can be swapped later (e.g. whisper.cpp) without
touching `api/voice.py`.
"""

from __future__ import annotations

import io
import logging
import time
from functools import lru_cache

import av
from av.container import InputContainer
from faster_whisper import WhisperModel
from pydantic import BaseModel

from config.settings import Settings
from db.connection import get_connection, get_read_only_engine, list_connection_names
from db.schema_introspection import introspect_schema
from voice.exceptions import VoiceInputTooLongError, VoiceProcessingError

logger = logging.getLogger(__name__)


class TranscriptionResult(BaseModel):
    """Output of one `transcribe()` call."""

    text: str
    stt_duration_ms: float


@lru_cache(maxsize=1)
def _get_whisper_model(model_size: str, device: str) -> WhisperModel:
    """Returns the process-wide Whisper model, creating it on first use.

    Cached the same way `agent.llm_client._get_ollama_client` and
    `db.connection._cached_engine` are -- a config change requires a
    process restart to take effect, consistent with every other
    process-lifetime singleton in this codebase. The model itself is
    auto-downloaded from Hugging Face Hub on first use and cached on disk
    by `faster-whisper`/`huggingface_hub`, the same "pull once, run
    offline after" shape as `ollama pull`.
    """
    logger.info("[voice] loading faster-whisper model size=%s device=%s", model_size, device)
    return WhisperModel(model_size, device=device)


def _probe_duration_seconds(audio_bytes: bytes) -> float:
    """Cheaply reads the decoded audio's duration without running Whisper.

    Used to reject an over-long recording before paying for the
    comparatively expensive transcription call -- see
    `Settings.voice_max_duration_seconds`.
    """
    try:
        with av.open(io.BytesIO(audio_bytes)) as container:
            # av.open()'s return type covers both read and write modes;
            # the default mode ("r") always returns an InputContainer,
            # which is the only variant with `.duration`.
            if not isinstance(container, InputContainer) or container.duration is None:
                raise VoiceProcessingError("Recorded audio has no readable duration.")
            return container.duration / av.time_base
    except VoiceProcessingError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any decode failure is a processing error
        raise VoiceProcessingError(f"Could not decode recorded audio: {exc}") from exc


def _build_vocabulary_hint(settings: Settings) -> str:
    """Builds a short, schema-derived vocabulary hint for Whisper's
    `initial_prompt` -- biases recognition toward real table/column names
    (e.g. "branch_id", "dispute", "merchant") instead of similar-sounding
    common words, across every configured database.

    Computed fresh per call: introspection reuses already-pooled
    connections (`db.connection.get_read_only_engine`) and is cheap enough
    at this call rate that a separate cache isn't worth the extra state --
    see this module's own "no extra cache layer" note in the design plan.
    Fails open (returns "") on any introspection error, matching this
    project's established pattern for every other accuracy-only aid (see
    `agent.llm_client._build_golden_examples_block`): a vocabulary hint is
    an accuracy improvement, never a reason transcription should fail.
    """
    terms: list[str] = []
    try:
        for name in list_connection_names(settings):
            connection = get_connection(settings, name)
            engine = get_read_only_engine(connection)
            for table in introspect_schema(engine, schema=connection.db_schema):
                terms.append(table.table_name)
                terms.extend(column.name for column in table.columns)
    except Exception:
        logger.warning(
            "[voice] vocabulary hint unavailable, transcribing without it", exc_info=True
        )
        return ""

    seen: set[str] = set()
    unique_terms: list[str] = []
    for term in terms:
        lowered = term.lower()
        if lowered not in seen:
            seen.add(lowered)
            unique_terms.append(term)

    hint = ""
    for term in unique_terms:
        candidate = f"{hint}, {term}" if hint else term
        if len(candidate) > settings.stt_vocabulary_max_chars:
            break
        hint = candidate
    return hint


def transcribe(audio_bytes: bytes, *, settings: Settings) -> TranscriptionResult:
    """Transcribes a recorded question to text.

    Args:
        audio_bytes: Raw recorded audio (any container `faster-whisper`'s
            PyAV dependency can decode -- e.g. the browser's default
            webm/opus). Already size-capped by the caller
            (`api/voice.py`, `Settings.voice_max_upload_mb`) before this
            is called.
        settings: Current process settings.

    Raises:
        VoiceInputTooLongError: if the decoded audio exceeds
            `Settings.voice_max_duration_seconds`.
        VoiceProcessingError: on any other decode/inference failure.
    """
    duration_seconds = _probe_duration_seconds(audio_bytes)
    if duration_seconds > settings.voice_max_duration_seconds:
        raise VoiceInputTooLongError(
            f"Recording is {duration_seconds:.1f}s, exceeding the "
            f"{settings.voice_max_duration_seconds}s cap."
        )

    started_at = time.perf_counter()
    vocabulary_hint = _build_vocabulary_hint(settings)
    try:
        model = _get_whisper_model(settings.stt_model_size, settings.stt_device)
        segments, _info = model.transcribe(
            io.BytesIO(audio_bytes),
            initial_prompt=vocabulary_hint or None,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:  # noqa: BLE001 -- any model failure is a processing error
        raise VoiceProcessingError(f"Transcription failed: {exc}") from exc

    return TranscriptionResult(
        text=text,
        stt_duration_ms=(time.perf_counter() - started_at) * 1000,
    )
