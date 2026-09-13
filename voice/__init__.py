"""Local speech-to-text and text-to-speech -- voice mode's whole backend.

Standalone library module, not imported by the core SQL pipeline
(`agent/graph.py`) at all -- only `api/voice.py` depends on it, and only
when `Settings.enable_voice_mode` is on. Both directions run fully local
inference (`faster-whisper` for STT, Piper for TTS), matching this
project's "Ollama, not a hosted LLM" posture: no cloud API, no data
leaving the machine, no API key required for voice mode either.

A transcribed question is never treated as anything other than plain,
untrusted text -- `api/voice.py`'s `POST /voice/transcribe` returns it to
the caller, which is expected to submit it back through the exact same
`POST /ask` path (and therefore `agent.input_guard.check_input`) a typed
question already goes through. Nothing here calls into `agent/` directly.
"""

from __future__ import annotations

from .exceptions import (
    VoiceError,
    VoiceInputTooLongError,
    VoiceModelNotFoundError,
    VoiceProcessingError,
)
from .stt import TranscriptionResult, transcribe
from .tts import SynthesisResult, synthesize

__all__ = [
    "SynthesisResult",
    "TranscriptionResult",
    "VoiceError",
    "VoiceInputTooLongError",
    "VoiceModelNotFoundError",
    "VoiceProcessingError",
    "synthesize",
    "transcribe",
]
