"""Video generation via IMA Studio (verified `text_to_video` task type --
see `media_gen/client.py`'s module docstring for the real API contract).
`image_to_video`/`first_last_frame_to_video`/`reference_image_to_video` are
real, confirmed task types too, but not wired up here -- this app has no
image-input flow yet (see `generate_image`'s "no model picker/no
reference-image input" simplification note)."""

from __future__ import annotations

from .client import IMAClient, MediaGenerationError, MediaResult, create_and_poll

# Video generation genuinely takes tens of seconds to minutes -- confirmed
# by IMA's own POLL_CONFIG (`VIDEO_MAX_WAIT_SECONDS = 40 * 60` in their
# reference config), not a guess.
_DEFAULT_POLL_INTERVAL_SECONDS = 8.0
_DEFAULT_POLL_TIMEOUT_SECONDS = 40 * 60.0


def generate_video(
    client: IMAClient,
    prompt: str,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout_seconds: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
) -> MediaResult:
    """Submit a text-to-video job and wait for it to finish."""
    try:
        media, model_name = create_and_poll(
            client,
            task_type="text_to_video",
            prompt=prompt,
            poll_interval_seconds=poll_interval_seconds,
            poll_timeout_seconds=poll_timeout_seconds,
        )
    except MediaGenerationError as exc:
        return MediaResult(status="failed", error=str(exc))

    url = media.get("url") or media.get("watermark_url") or media.get("preview_url")
    return MediaResult(status="completed", url=url, model=model_name)
