"""Provider-agnostic AI-guided image editing -- the seam
`attachments/ai_edit.py` calls into, and the one place a real generative
provider is wired in (or swapped out) without touching orchestration,
validation, storage, or API-layer code.

**Real, live-verified provider research (2026-09-27), not assumed:**
fetched the real reference source
(`github.com/imastuido/ima-all-ai`'s `scripts/ima_runtime/` and
`capabilities/image/` docs -- the same repo `media_gen/client.py`'s own
module docstring already cites as ground truth) and made a real, read-only,
no-cost `GET /open/v1/product/list?category=image_to_image` call against
this project's actual configured IMA account. Confirmed:

- `image_to_image` is a real, working IMA task category on this account,
  with 5 available models (`gpt-image-2`, `gemini-3.1-flash-image` "Nano
  Banana 2", `gemini-3-pro-image` "Nano Banana Pro", `doubao-seedream-4.5`,
  `midjourney`).
- **None of their `form_config` fields expose a native mask/inpainting
  parameter.** These are instruction-driven, whole-image (or
  whole-reference-image) editors, not pixel-level inpainting APIs.
- A source image must be a real, publicly fetchable URL (`src_img_url`
  top-level + `input_images` inner, per the verified reference doc) --
  never raw bytes/base64 in the create payload. `media_gen/upload.py`
  implements the real, verified two-step upload (a signed
  `imapi.liveme.com` upload-token request, then a `PUT` of the raw bytes)
  that turns local bytes into such a URL.

**Consequence for mask handling, stated honestly rather than assumed
away**: since this provider has no native mask channel, a user-painted
mask (this app's own canonical convention -- see `attachments.mask`'s
docstring) is conveyed to IMA as a **visual overlay baked into the image
pixels themselves** (a translucent red highlight over the editable region)
**plus an explicit textual instruction** telling the model to only modify
the highlighted area. This is a real, working technique for
instruction-only image editors, but it is emphatically not the same
guarantee a native alpha-channel mask would give -- the result is
disclosed to the caller via `ImageEditResult.warnings` every time a mask
was actually used, never silently presented as pixel-exact masking.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Literal, Protocol

from PIL import Image, UnidentifiedImageError

from config.settings import Settings

logger = logging.getLogger(__name__)

# The operation allowlist for AI-guided (generative) editing -- deliberately
# narrower than "any string the client sends." "enhance" is included here,
# labeled as AI-based rather than a deterministic filter (see CLAUDE.md's
# quick-action table: "Enhance selected region: Deterministic enhancement
# or explicit AI edit, label which is used" -- this app's own choice is the
# latter, disclosed plainly, not a silent claim of determinism). Deliberately
# excludes anything resembling "remove watermark" -- CLAUDE.md's own
# instruction not to silently treat that as an unrestricted quick action;
# this app simply does not offer it as a preset or an allowed operation at
# all, a deliberate product/legal-policy scope decision, not an oversight.
ImageEditOperation = Literal[
    "remove_object", "replace_background", "replace_sky", "region_edit", "enhance"
]
ALLOWED_IMAGE_EDIT_OPERATIONS: frozenset[str] = frozenset(
    {"remove_object", "replace_background", "replace_sky", "region_edit", "enhance"}
)

_MASK_OVERLAY_WARNING = (
    "This provider has no native pixel-level mask channel -- the selected region was "
    "conveyed to the model as a highlighted overlay plus an explicit instruction, not a "
    "guaranteed pixel mask. Details just outside the highlighted area may still shift."
)

_MASK_OVERLAY_INSTRUCTION = (
    "Only modify the region highlighted in translucent red in this image. Preserve "
    "everything outside the highlighted region exactly as-is: same composition, lighting, "
    "colors, and detail. "
)


def _build_fake_success_png() -> bytes:
    """A tiny (2x2), genuinely valid PNG -- `FakeImageEditProvider`'s
    default "success" output, so a test exercising the full decode/
    validate/store path (`register_derived_image` re-decodes and
    re-normalizes every stored image via Pillow) receives real, decodable
    bytes rather than a hand-typed byte literal, which is exactly the kind
    of transcription error this generates-it-with-Pillow approach avoids."""
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


_FAKE_SUCCESS_PNG = _build_fake_success_png()


class ImageEditProviderError(RuntimeError):
    """Raised for any provider-side failure -- a business error, a network
    failure, a timeout, or bad/undecodable output. Same two-message
    convention as `media_gen.client.MediaGenerationError`: `str(exc)` is the
    full internal detail (safe to log), `.safe_message` is a short,
    user-facing sentence that never leaks a raw provider payload."""

    def __init__(self, message: str, *, safe_message: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.safe_message = safe_message or (
            "Image editing failed due to a provider error. Please try again in a moment."
        )
        self.retryable = retryable


@dataclass(frozen=True)
class ImageEditRequest:
    """Everything one edit provider call needs. `image_bytes`/`mask_bytes`
    are always real, already-decoded-and-validated bytes by the time this
    reaches a provider -- never a filename, a blob URL, or a client-supplied
    path (see `attachments/ai_edit.py`'s own docstring for the validation
    that happens before this is constructed)."""

    image_bytes: bytes
    image_content_type: str
    prompt: str
    operation: str
    mask_bytes: bytes | None = None


@dataclass(frozen=True)
class ImageEditResult:
    status: Literal["completed", "failed"]
    image_bytes: bytes | None = None
    media_type: str | None = None
    provider: str = "ima_studio"
    model: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None


class ImageEditProvider(Protocol):
    """The seam every adapter implements -- `attachments/ai_edit.py` only
    ever calls this, never a concrete provider class directly."""

    def edit(self, request: ImageEditRequest) -> ImageEditResult: ...


class FakeImageEditProvider:
    """Test double -- never makes a network call. Records every request it
    receives (`self.requests`), including the real `image_bytes`/
    `mask_bytes` (not filenames or placeholders), so a test can assert the
    actual bytes it would send a real provider -- per this feature's own
    "mocked provider tests must assert image and mask bytes are passed, not
    merely filenames" requirement.
    """

    def __init__(
        self,
        *,
        result: ImageEditResult | None = None,
        error: ImageEditProviderError | None = None,
    ) -> None:
        self.requests: list[ImageEditRequest] = []
        self._result = result
        self._error = error

    def edit(self, request: ImageEditRequest) -> ImageEditResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        return ImageEditResult(
            status="completed",
            image_bytes=_FAKE_SUCCESS_PNG,
            media_type="image/png",
            provider="fake",
            model="fake-edit-model",
        )


def _composite_mask_overlay(image_bytes: bytes, mask_bytes: bytes) -> bytes:
    """Bakes the canonical mask (see `attachments.mask`'s docstring: white/
    opaque = editable) into `image_bytes` as a translucent red highlight --
    the real, disclosed mask-conveyance mechanism for a provider with no
    native mask channel. See this module's own docstring for why."""
    from attachments.mask import decode_and_validate_mask

    with Image.open(io.BytesIO(image_bytes)) as src:
        source = src.convert("RGBA")
    decoded = decode_and_validate_mask(
        mask_bytes, target_width=source.width, target_height=source.height
    )
    mask_channel = Image.fromarray(decoded.array, mode="L")

    overlay = Image.new("RGBA", source.size, (255, 0, 0, 0))
    # Alpha proportional to mask intensity, capped well short of fully
    # opaque -- visible enough for the model to reliably locate the
    # highlighted region while the underlying content underneath is still
    # legible (a fully-opaque overlay would hide exactly the content the
    # instruction needs the model to reason about).
    alpha = mask_channel.point(lambda p: int(p * 0.45))
    overlay.putalpha(alpha)
    composited = Image.alpha_composite(source, overlay)

    buffer = io.BytesIO()
    composited.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _extension_for_content_type(content_type: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(content_type, "png")


class ImaImageEditProvider:
    """The real adapter -- IMA Studio's `image_to_image` task category. See
    this module's own docstring for the full, live-verified provider
    research this is built from."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def edit(self, request: ImageEditRequest) -> ImageEditResult:
        from media_gen.client import MediaGenerationError, create_and_poll, get_ima_client
        from media_gen.download import download_media_bytes
        from media_gen.upload import upload_image_to_ima

        if request.operation not in ALLOWED_IMAGE_EDIT_OPERATIONS:
            raise ImageEditProviderError(
                f"Unsupported image-edit operation: {request.operation!r}",
                safe_message="That editing operation isn't supported.",
            )

        try:
            client = get_ima_client(self._settings)
        except (
            Exception
        ) as exc:  # noqa: BLE001 - MediaGenerationNotConfiguredError, a ConfigurationError subclass
            raise ImageEditProviderError(
                str(exc), safe_message="AI-guided image editing is not configured on this server."
            ) from exc

        image_bytes = request.image_bytes
        prompt = request.prompt
        warnings: list[str] = []
        if request.mask_bytes:
            try:
                image_bytes = _composite_mask_overlay(request.image_bytes, request.mask_bytes)
            except (
                Exception
            ) as exc:  # noqa: BLE001 - ImageDecodeError/MaskValidationError, both real rejections
                raise ImageEditProviderError(
                    f"Could not apply mask overlay: {exc}", safe_message=str(exc)
                ) from exc
            prompt = _MASK_OVERLAY_INSTRUCTION + prompt
            warnings.append(_MASK_OVERLAY_WARNING)

        api_key = (
            self._settings.ima_api_key.get_secret_value() if self._settings.ima_api_key else ""
        )
        try:
            uploaded_url = upload_image_to_ima(
                api_key,
                image_bytes,
                request.image_content_type,
                suffix=_extension_for_content_type(request.image_content_type),
            )
        except MediaGenerationError as exc:
            raise ImageEditProviderError(str(exc), safe_message=exc.safe_message) from exc

        try:
            media, model_name = create_and_poll(
                client,
                "image_to_image",
                prompt,
                poll_interval_seconds=self._settings.image_edit_poll_interval_seconds,
                poll_timeout_seconds=float(self._settings.image_edit_timeout_seconds),
                image_urls=[uploaded_url],
            )
        except MediaGenerationError as exc:
            logger.warning("[image_edit] IMA image_to_image call failed: %s", exc)
            raise ImageEditProviderError(str(exc), safe_message=exc.safe_message) from exc

        result_url = media.get("url") or media.get("watermark_url") or media.get("preview_url")
        if not result_url:
            raise ImageEditProviderError(
                f"IMA image edit completed with no result URL: {media}",
                safe_message="The edit completed but no result could be retrieved.",
            )

        try:
            data, content_type = download_media_bytes(
                result_url, max_bytes=self._settings.media_max_file_mb * 1024 * 1024
            )
        except MediaGenerationError as exc:
            raise ImageEditProviderError(
                str(exc),
                safe_message="The edit completed but the result image could not be retrieved.",
            ) from exc

        # Treat generated bytes as untrusted, always -- decode/verify before
        # this result is ever handed to the storage layer (same principle
        # `attachments.image_processing.normalize_image` already applies to
        # every uploaded attachment).
        try:
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise ImageEditProviderError(
                f"Provider returned bytes that do not decode as a valid image: {exc}",
                safe_message="The edited image could not be validated and was discarded.",
            ) from exc

        logger.info(
            "[image_edit] operation=%s provider=ima_studio model=%s mask=%s",
            request.operation,
            model_name,
            request.mask_bytes is not None,
        )
        return ImageEditResult(
            status="completed",
            image_bytes=data,
            media_type=content_type,
            provider="ima_studio",
            model=model_name,
            warnings=tuple(warnings),
        )


__all__ = [
    "ALLOWED_IMAGE_EDIT_OPERATIONS",
    "FakeImageEditProvider",
    "ImageEditOperation",
    "ImageEditProvider",
    "ImageEditProviderError",
    "ImageEditRequest",
    "ImageEditResult",
    "ImaImageEditProvider",
]
