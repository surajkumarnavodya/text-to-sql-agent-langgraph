"""Custom exceptions for the voice layer.

Same two-message convention as `agent/exceptions.py::AgentError` (not a
subclass of it -- voice mode isn't agent-graph logic, it's a standalone
capability `api/voice.py` calls before a question ever reaches the agent):
`str(exc)` is the full internal detail for logs, `.safe_message` is a
short, non-technical sentence safe to put directly in an API response.
"""

from __future__ import annotations

_DEFAULT_SAFE_MESSAGE = (
    "Voice mode is temporarily unavailable. Please try again or type your question."
)


class VoiceError(Exception):
    """Base class for all voice-layer errors."""

    _default_safe_message: str = _DEFAULT_SAFE_MESSAGE

    def __init__(self, detail: str, *, safe_message: str | None = None) -> None:
        super().__init__(detail)
        self.safe_message = safe_message or self._default_safe_message


class VoiceModelNotFoundError(VoiceError):
    """Raised when the configured Piper voice model file isn't present."""

    _default_safe_message = (
        "The voice model hasn't been downloaded yet. Run "
        "`python scripts/download_voice_model.py` and try again."
    )


class VoiceInputTooLongError(VoiceError):
    """Raised when a recorded question exceeds `Settings.voice_max_duration_seconds`."""

    _default_safe_message = "That recording is too long. Please ask a shorter question."


class VoiceProcessingError(VoiceError):
    """Raised for any other STT/TTS runtime failure (corrupt audio, model error, ...)."""
