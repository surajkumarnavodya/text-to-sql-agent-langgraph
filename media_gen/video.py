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
    duration_seconds: int | None = None,
) -> MediaResult:
    """Submit a text-to-video job and wait for it to finish.

    `duration_seconds`, if given, overrides the selected model's own
    default "duration" form field (`Settings.media_gen_video_duration_seconds`
    is the config-driven source of this). This does NOT mean any length is
    achievable -- verified live against this account's actual auto-selected
    model (`GET /open/v1/product/list?category=text_to_video`, read-only,
    no cost): "Seedance 2.0" exposes `duration` as an integer 4-15 (its own
    `form_config` entry's `config: {"minimum": 4, "maximum": 15}`), default
    5. There is no IMA video model on this account (or, as far as this
    project has confirmed, offered by IMA at all) capable of a single
    multi-minute generation -- current text-to-video models generally
    cap in the 5-15 second range per call, a real model-capability limit,
    not a configuration restriction this app imposes. A value outside the
    selected model's real range is rejected by IMA itself (a business
    error, surfaced as a clean `MediaResult(status="failed")` like any
    other provider rejection) -- this function does not pre-validate
    against the live per-model min/max.
    """
    try:
        media, model_name = create_and_poll(
            client,
            task_type="text_to_video",
            prompt=prompt,
            poll_interval_seconds=poll_interval_seconds,
            poll_timeout_seconds=poll_timeout_seconds,
            form_overrides={"duration": duration_seconds} if duration_seconds is not None else None,
        )
    except MediaGenerationError as exc:
        return MediaResult(status="failed", error=exc.safe_message, detail=str(exc))

    url = media.get("url") or media.get("watermark_url") or media.get("preview_url")
    return MediaResult(status="completed", url=url, model=model_name)
