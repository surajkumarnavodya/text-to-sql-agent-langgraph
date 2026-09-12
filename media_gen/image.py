"""Image generation via IMA Studio (verified `text_to_image` task type --
see `media_gen/client.py`'s module docstring for the real API contract this
implements, including the "no model picker" simplification)."""

from __future__ import annotations

from .client import IMAClient, MediaGenerationError, MediaResult, create_and_poll


def generate_image(
    client: IMAClient,
    prompt: str,
    poll_interval_seconds: float = 5.0,
    poll_timeout_seconds: float = 600.0,
) -> MediaResult:
    """Generate an image from a text prompt. Real flow (confirmed against
    IMA's own reference implementation): discover an available model via
    `GET /open/v1/product/list`, `POST /open/v1/tasks/create`, then poll
    `POST /open/v1/tasks/detail` until the media's `resource_status`
    reaches a terminal state -- image generation is asynchronous on this
    API, not a single synchronous call."""
    try:
        media, model_name = create_and_poll(
            client,
            task_type="text_to_image",
            prompt=prompt,
            poll_interval_seconds=poll_interval_seconds,
            poll_timeout_seconds=poll_timeout_seconds,
        )
    except MediaGenerationError as exc:
        return MediaResult(status="failed", error=str(exc))

    url = media.get("url") or media.get("watermark_url") or media.get("preview_url")
    return MediaResult(status="completed", url=url, model=model_name)
