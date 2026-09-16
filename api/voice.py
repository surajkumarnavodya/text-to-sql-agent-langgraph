"""FastAPI router for voice mode: speech-to-text and text-to-speech.

Both routes 404 immediately when `Settings.enable_voice_mode` is off, the
same "an enabled flag with nothing behind it is not available" pattern
`api/documents.py`'s `_require_collection_configured` already uses.
Neither route calls into `agent/` at all: `POST /voice/transcribe` returns
plain text for the caller to submit through the ordinary `POST /ask` path
(so `agent.input_guard.check_input` still applies unconditionally, exactly
as it does to a typed question), and `POST /voice/synthesize` only ever
reads back an already-produced answer string. `POST /voice/transcribe`
returns Whisper's raw output *and* a `voice.correction.correct_transcript`
suggestion (misheard-word/filler-word/self-correction cleanup, gated by
`Settings.enable_voice_correction`) side by side -- the frontend shows
both and requires an explicit user action (never an automatic timer) to
pick one and submit it. Both are plain, untrusted text either way, never
pre-validated or treated as an instruction to this app.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response

from agent.authz import Permission
from api.authz import require_permission
from api.rate_limit import enforce_api_action_rate_limit
from api.schemas import SynthesizeRequest, TranscribeResponse
from config.settings import get_settings
from voice.correction import correct_transcript
from voice.exceptions import VoiceInputTooLongError, VoiceModelNotFoundError, VoiceProcessingError
from voice.stt import transcribe
from voice.tts import synthesize

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_permission(Permission.VOICE_USE))])


def _require_voice_mode_enabled() -> None:
    if not get_settings().enable_voice_mode:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Voice mode is disabled. Set ENABLE_VOICE_MODE=true in .env.",
        )


@router.post("/voice/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(request: Request, audio: UploadFile = File(...)) -> TranscribeResponse:
    """Transcribes a recorded question to text.

    Rate-limited per client IP (`enforce_api_action_rate_limit`) --
    transcription is real, non-trivial CPU work. Reads at most
    `voice_max_upload_mb + 1` bytes regardless of what the upload claims
    (the same bounded-read pattern `api/documents.py::upload_document`
    already uses), then rejects anything over that cap before it ever
    reaches the STT model. The returned text is never passed to the agent
    here -- see this module's docstring. The raw Whisper text is also run
    through `voice.correction.correct_transcript` (fails open to the raw
    text unchanged on any Ollama error) to produce `corrected_text` --
    both are returned; nothing is submitted automatically.
    """
    settings = get_settings()
    _require_voice_mode_enabled()
    enforce_api_action_rate_limit(request, "voice_transcribe", settings)

    max_bytes = settings.voice_max_upload_mb * 1024 * 1024
    audio_bytes = await audio.read(max_bytes + 1)
    if len(audio_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Recording exceeds the {settings.voice_max_upload_mb}MB upload limit.",
        )

    try:
        result = transcribe(audio_bytes, settings=settings)
    except VoiceInputTooLongError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=exc.safe_message
        ) from exc
    except VoiceProcessingError as exc:
        logger.warning("[voice] transcription failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.safe_message
        ) from exc

    corrected = correct_transcript(result.text, settings=settings)
    corrected_text = corrected if corrected != result.text else None

    logger.info("[voice] transcribed in %.0fms", result.stt_duration_ms)
    return TranscribeResponse(
        text=result.text, corrected_text=corrected_text, stt_duration_ms=result.stt_duration_ms
    )


@router.post("/voice/synthesize")
def synthesize_speech(payload: SynthesizeRequest, request: Request) -> Response:
    """Synthesizes text to speech (WAV bytes).

    `text` length is capped at `Settings.max_question_length` -- the same
    bound `AskRequest.question` already relies on, reused rather than
    duplicated. Rate-limited per client IP, same pattern as every other
    action route in this app.
    """
    settings = get_settings()
    _require_voice_mode_enabled()
    enforce_api_action_rate_limit(request, "voice_synthesize", settings)

    if len(payload.text) > settings.max_question_length:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Text exceeds the {settings.max_question_length}-character limit.",
        )

    try:
        result = synthesize(payload.text, settings=settings)
    except VoiceModelNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.safe_message
        ) from exc
    except VoiceProcessingError as exc:
        logger.warning("[voice] synthesis failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.safe_message
        ) from exc

    logger.info("[voice] synthesized in %.0fms", result.tts_duration_ms)
    return Response(content=result.audio_bytes, media_type="audio/wav")
