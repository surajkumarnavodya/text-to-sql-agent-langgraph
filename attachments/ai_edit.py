"""AI-guided (generative) image editing -- the isolated editing service
`POST /attachments/{id}/ai-edit` calls into. Deliberately its own module,
never folded into `attachments/image_ops.py` (deterministic resize) or
`attachments/inpaint.py` (classical, non-generative text removal): this is
the one attachment-image operation that makes a real, metered, external
provider call, and it needs the safety/rate-limit/audit posture that
implies -- mirroring `agent.orchestrator.nodes.execute_generation`'s own
shape (safety check -> rate limit -> provider call -> validate output),
applied to an attachment-derived source image instead of a fresh
text-to-image prompt.

**Never touches the LangGraph SQL pipeline, schema retrieval, or SQL
validation in any way** -- this module has no `agent.graph`/`agent.nodes`
import at all. An image-edit request is answered entirely here; see
`agent/orchestrator/nodes.py`'s own attachment-routing guard
(`_looks_like_attachment_only_question`) for the separate, existing
mechanism that keeps an attachment-only *chat* question from also
triggering SQL generation -- this module doesn't need an equivalent guard
itself, since it's reached only via its own dedicated REST route, never
via `/ask`.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from agent.rate_limit import MEDIA_GENERATION_LIMIT_MESSAGE, get_media_generation_limiter
from attachments.image_processing import ImageDecodeError
from attachments.mask import MaskValidationError, decode_and_validate_mask, mask_array_to_png_bytes
from config.settings import Settings
from media_gen.content_policy import basic_prompt_safety_check
from media_gen.image_edit_provider import (
    ALLOWED_IMAGE_EDIT_OPERATIONS,
    ImageEditProvider,
    ImageEditProviderError,
    ImageEditRequest,
    ImageEditResult,
    ImaImageEditProvider,
)
from security.audit_log import log_security_event

logger = logging.getLogger(__name__)


class ImageEditNotConfiguredError(RuntimeError):
    """Raised when AI-guided image editing is disabled or `IMA_API_KEY`
    isn't set -- mirrors `media_gen.client.MediaGenerationNotConfiguredError`
    exactly, a distinct type so `api/attachments.py` can return a clean,
    honest "not available" response rather than a generic 500."""


def get_image_edit_provider(settings: Settings) -> ImageEditProvider:
    """The one place a real provider is constructed -- swapping providers
    later means changing only this function, never
    `execute_image_edit`/the API route. Raises `ImageEditNotConfiguredError`
    unless both `Settings.enable_image_editing` and `Settings.ima_api_key`
    are set, mirroring `enable_media_generation`/`ima_api_key`'s own
    "both must be true/present" pattern (see `Settings.enable_image_editing`'s
    own docstring for why this is a separate flag from media generation)."""
    if not settings.enable_image_editing or not settings.ima_api_key:
        raise ImageEditNotConfiguredError(
            "AI-guided image editing is disabled or IMA_API_KEY is not set. Set "
            "ENABLE_IMAGE_EDITING=true and IMA_API_KEY in your .env to enable it."
        )
    return ImaImageEditProvider(settings)


@dataclass(frozen=True)
class ImageEditOutcome:
    """What `execute_image_edit` returns -- the API route builds its
    response directly from this, never re-deriving anything from the raw
    provider result itself."""

    status: str  # "completed" | "failed"
    operation: str
    mask_provided: bool
    image_bytes: bytes | None = None
    media_type: str | None = None
    provider: str | None = None
    model: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    error_code: str | None = None
    error_message: str | None = None


def _failed(operation: str, mask_provided: bool, code: str, message: str) -> ImageEditOutcome:
    return ImageEditOutcome(
        status="failed",
        operation=operation,
        mask_provided=mask_provided,
        error_code=code,
        error_message=message,
    )


# Bounded (mirrors `attachments.store.AttachmentStore`'s own FIFO-eviction
# reasoning) idempotency cache, keyed by `(owner_subject, idempotency_key)`
# -- a retried request (e.g. a client double-submit, or a network-level
# retry after a slow response) with the *same* key returns the already-
# computed outcome instead of paying for a second real provider call. TTL
# is enforced lazily (checked on lookup, not by a background sweep) --
# consistent with every other single-process, in-memory cache in this
# codebase (`media_gen.cache.MediaCache`, `attachments.store.AttachmentStore`)
# already disclosing the same "process-lifetime only, no cross-instance
# sharing" limitation.
_IDEMPOTENCY_TTL_SECONDS = 300.0
_IDEMPOTENCY_MAX_ENTRIES = 200
_idempotency_cache: OrderedDict[tuple[str, str], tuple[float, ImageEditOutcome]] = OrderedDict()


def _idempotency_key_lookup(
    owner_subject: str | None, idempotency_key: str | None
) -> ImageEditOutcome | None:
    if not idempotency_key:
        return None
    key = (owner_subject or "", idempotency_key)
    entry = _idempotency_cache.get(key)
    if entry is None:
        return None
    stored_at, outcome = entry
    if time.monotonic() - stored_at > _IDEMPOTENCY_TTL_SECONDS:
        del _idempotency_cache[key]
        return None
    return outcome


def _idempotency_key_store(
    owner_subject: str | None, idempotency_key: str | None, outcome: ImageEditOutcome
) -> None:
    if not idempotency_key:
        return
    key = (owner_subject or "", idempotency_key)
    if key in _idempotency_cache:
        del _idempotency_cache[key]
    elif len(_idempotency_cache) >= _IDEMPOTENCY_MAX_ENTRIES:
        _idempotency_cache.popitem(last=False)
    _idempotency_cache[key] = (time.monotonic(), outcome)


def execute_image_edit(
    *,
    source_bytes: bytes,
    source_content_type: str,
    source_width: int,
    source_height: int,
    operation: str,
    prompt: str,
    mask_bytes: bytes | None,
    settings: Settings,
    owner_subject: str | None = None,
    idempotency_key: str | None = None,
    provider: ImageEditProvider | None = None,
) -> ImageEditOutcome:
    """Validates, rate-limits, and executes one AI-guided image edit.

    `provider` is injectable for tests (a `FakeImageEditProvider`); `None`
    (the default) resolves the real configured provider via
    `get_image_edit_provider`, which is exactly how `api/attachments.py`'s
    route calls this in production -- there is no second code path that
    skips validation for the "real" case.

    Never raises for an ordinary failure (unsupported operation, provider
    unavailable, provider error, undecodable mask/output) -- every one of
    those becomes a `status="failed"` `ImageEditOutcome` with a safe
    `error_code`/`error_message`, the same fail-closed-but-never-crash
    contract `agent.orchestrator.nodes.execute_generation` already
    establishes for media generation. A genuine programming error (a bug)
    still propagates, deliberately -- this function does not have a bare
    `except Exception` swallowing everything.
    """
    mask_provided = mask_bytes is not None

    cached = _idempotency_key_lookup(owner_subject, idempotency_key)
    if cached is not None:
        logger.info("[ai_edit] idempotency key hit -- reusing cached outcome, no provider call")
        return cached

    if operation not in ALLOWED_IMAGE_EDIT_OPERATIONS:
        return _failed(
            operation,
            mask_provided,
            "unsupported_operation",
            "That editing operation isn't supported.",
        )

    if not prompt.strip():
        return _failed(operation, mask_provided, "empty_prompt", "An edit instruction is required.")
    if len(prompt) > settings.image_edit_max_prompt_length:
        return _failed(
            operation,
            mask_provided,
            "prompt_too_long",
            f"The edit instruction may not exceed {settings.image_edit_max_prompt_length} characters.",
        )

    safety_rejection = basic_prompt_safety_check(prompt)
    if safety_rejection:
        log_security_event("image_edit_rejected", "warning", safety_rejection, operation=operation)
        return _failed(operation, mask_provided, "content_policy_rejected", safety_rejection)

    decoded_mask_bytes: bytes | None = None
    if mask_bytes is not None:
        try:
            decoded = decode_and_validate_mask(
                mask_bytes, target_width=source_width, target_height=source_height
            )
        except ImageDecodeError as exc:
            return _failed(operation, mask_provided, "invalid_mask", str(exc))
        except MaskValidationError as exc:
            return _failed(operation, mask_provided, "empty_mask", str(exc))
        decoded_mask_bytes = mask_array_to_png_bytes(decoded.array)

    limiter = get_media_generation_limiter(
        settings.media_gen_rate_limit, settings.media_gen_rate_window_seconds
    )
    limit_result = limiter.check()
    if not limit_result.allowed:
        log_security_event(
            "image_edit_rate_limited",
            "info",
            MEDIA_GENERATION_LIMIT_MESSAGE,
            operation=operation,
            retry_after_seconds=round(limit_result.retry_after_seconds, 1),
        )
        return _failed(operation, mask_provided, "rate_limited", MEDIA_GENERATION_LIMIT_MESSAGE)

    try:
        resolved_provider = provider or get_image_edit_provider(settings)
    except ImageEditNotConfiguredError as exc:
        logger.info("[ai_edit] not configured: %s", exc)
        return _failed(operation, mask_provided, "not_configured", str(exc))

    request = ImageEditRequest(
        image_bytes=source_bytes,
        image_content_type=source_content_type,
        prompt=prompt.strip(),
        operation=operation,
        mask_bytes=decoded_mask_bytes,
    )
    try:
        result: ImageEditResult = resolved_provider.edit(request)
    except ImageEditProviderError as exc:
        logger.warning("[ai_edit] provider error for operation=%s: %s", operation, exc)
        log_security_event("image_edit_provider_failed", "warning", str(exc), operation=operation)
        return _failed(operation, mask_provided, "provider_error", exc.safe_message)

    if result.status != "completed" or not result.image_bytes:
        message = result.error or "Image editing failed due to a provider error."
        return _failed(operation, mask_provided, "provider_error", message)

    log_security_event(
        "image_edit_succeeded",
        "info",
        "An AI-guided image-edit request completed and produced a real, metered provider call.",
        operation=operation,
        provider=result.provider,
        model=result.model,
        mask_provided=mask_provided,
    )
    outcome = ImageEditOutcome(
        status="completed",
        operation=operation,
        mask_provided=mask_provided,
        image_bytes=result.image_bytes,
        media_type=result.media_type,
        provider=result.provider,
        model=result.model,
        warnings=result.warnings,
    )
    _idempotency_key_store(owner_subject, idempotency_key, outcome)
    return outcome


__all__ = [
    "ImageEditNotConfiguredError",
    "ImageEditOutcome",
    "execute_image_edit",
    "get_image_edit_provider",
]
