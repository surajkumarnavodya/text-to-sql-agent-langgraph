"""Video-audio transcription for media search -- reuses `voice/stt.py`'s
faster-whisper model loader directly (`voice.stt.get_whisper_model`) rather
than loading a second Whisper model instance, since it's the same backend
doing the same job. Unlike `voice/stt.py::transcribe` (one flat string for
a short recorded question), this returns per-Whisper-segment text with
timestamps, so `media/ingest.py` can bucket each video segment's own
transcript by time-range overlap -- a video's transcript needs to cite
*which part* said what, not just "the video mentions X somewhere."

`faster-whisper`'s own audio decoding (`av`/PyAV, already a transitive
dependency via `faster-whisper` itself -- confirmed by reading
`faster_whisper.audio.decode_audio`'s source before relying on this) pulls
the audio stream directly out of a video container via `container.decode
(audio=0)`, ignoring any video stream -- no separate audio-extraction step
is needed; the video file's own path is passed straight to
`model.transcribe()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings
from voice.stt import get_whisper_model

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptSegment:
    """One Whisper-decoded segment of a video's audio track."""

    start: float
    end: float
    text: str


def transcribe_video(video_path: Path, settings: Settings) -> list[TranscriptSegment]:
    """Transcribes a video's audio track, returning timestamped segments.

    Fails open (returns `[]`, logs a warning) on any decode/inference
    failure -- a video with no audio track, a corrupt/unsupported audio
    codec, or a transcription error should still be indexed via its visual
    content (keyframes/OCR/captions) alone, matching `media/ocr.py`'s and
    `media/captioning.py`'s own fail-open posture.
    """
    try:
        model = get_whisper_model(settings)
        segments, _info = model.transcribe(str(video_path))
        return [
            TranscriptSegment(start=segment.start, end=segment.end, text=segment.text.strip())
            for segment in segments
            if segment.text.strip()
        ]
    except Exception:
        logger.warning(
            "[media] transcription unavailable for %s, indexing without it",
            video_path,
            exc_info=True,
        )
        return []


def transcript_for_range(segments: list[TranscriptSegment], start: float, end: float) -> str:
    """Joins every transcript segment whose midpoint falls within
    `[start, end)` -- used to assign each Whisper-decoded segment to the
    one scene-detected `KeyframeSegment` (`media/keyframes.py`) it belongs
    to, since the two are decoded independently and don't share
    boundaries."""
    matching = [
        segment.text for segment in segments if start <= (segment.start + segment.end) / 2 < end
    ]
    return " ".join(matching).strip()
