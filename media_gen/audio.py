"""Audio generation via IMA Studio (verified `text_to_speech`/`text_to_music`
task types -- see `media_gen/client.py`'s module docstring for the real API
contract). Poll timings below match IMA's own reference `POLL_CONFIG` for
these two task types, not a guess."""

from __future__ import annotations

from typing import Literal

from .client import IMAClient, MediaGenerationError, MediaResult, create_and_poll

_POLL_DEFAULTS = {
    "speech": {"interval_seconds": 3.0, "timeout_seconds": 300.0},
    "music": {"interval_seconds": 5.0, "timeout_seconds": 480.0},
}


def generate_audio(
    client: IMAClient,
    text: str,
    mode: Literal["speech", "music"] = "speech",
) -> MediaResult:
    """`mode="speech"` -> text-to-speech narration of `text` (e.g. read a
    SQL answer back to the user); `mode="music"` -> treat `text` as a
    music/lyrics prompt. Both are real, confirmed task types
    (`text_to_speech`/`text_to_music`) on the same create+poll flow as
    image/video."""
    task_type = "text_to_speech" if mode == "speech" else "text_to_music"
    poll = _POLL_DEFAULTS[mode]

    try:
        media, model_name = create_and_poll(
            client,
            task_type=task_type,
            prompt=text,
            poll_interval_seconds=poll["interval_seconds"],
            poll_timeout_seconds=poll["timeout_seconds"],
        )
    except MediaGenerationError as exc:
        return MediaResult(status="failed", error=exc.safe_message, detail=str(exc))

    url = media.get("url") or media.get("watermark_url") or media.get("preview_url")
    return MediaResult(status="completed", url=url, model=model_name)
