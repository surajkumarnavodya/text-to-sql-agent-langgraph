"""Text-to-speech: wraps Piper behind a single `synthesize()` call, so the
backend can be swapped later (e.g. Coqui TTS) without touching `api/voice.py`.
"""

from __future__ import annotations

import io
import logging
import time
import wave
from functools import lru_cache
from pathlib import Path

from piper import PiperVoice
from pydantic import BaseModel

from config.settings import Settings
from voice.exceptions import VoiceModelNotFoundError, VoiceProcessingError

logger = logging.getLogger(__name__)


class SynthesisResult(BaseModel):
    """Output of one `synthesize()` call."""

    audio_bytes: bytes
    tts_duration_ms: float


def _default_model_path(voice_name: str) -> Path:
    return Path(__file__).resolve().parent / "models" / f"{voice_name}.onnx"


@lru_cache(maxsize=1)
def _get_piper_voice(voice_name: str, model_path: str | None) -> PiperVoice:
    """Returns the process-wide Piper voice, loading it on first use --
    same cached-singleton pattern as `voice.stt._get_whisper_model`.

    Args:
        voice_name: `Settings.tts_voice`.
        model_path: `str(Settings.tts_voice_model_path)` if set, else
            `None` to use the default `voice/models/<voice_name>.onnx`
            location -- passed as a plain string (not a `Settings`
            instance) so this stays hashable for `lru_cache`.
    """
    resolved = Path(model_path) if model_path else _default_model_path(voice_name)
    if not resolved.is_file():
        raise VoiceModelNotFoundError(
            f"Piper voice model not found at {resolved}. Run "
            "`python scripts/download_voice_model.py` to download it."
        )
    logger.info("[voice] loading Piper voice %s from %s", voice_name, resolved)
    return PiperVoice.load(resolved)


def synthesize(text: str, *, settings: Settings) -> SynthesisResult:
    """Synthesizes `text` to speech (WAV bytes).

    Raises:
        VoiceModelNotFoundError: if the configured voice model file is missing.
        VoiceProcessingError: on any other synthesis failure.
    """
    model_path = str(settings.tts_voice_model_path) if settings.tts_voice_model_path else None
    voice = _get_piper_voice(settings.tts_voice, model_path)

    started_at = time.perf_counter()
    try:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        wav_bytes = buffer.getvalue()
    except VoiceModelNotFoundError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any synthesis failure is a processing error
        raise VoiceProcessingError(f"Speech synthesis failed: {exc}") from exc

    return SynthesisResult(
        audio_bytes=wav_bytes,
        tts_duration_ms=(time.perf_counter() - started_at) * 1000,
    )
