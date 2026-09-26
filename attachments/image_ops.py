"""Deterministic image manipulation -- resize only, Pillow-based, no model
call and no LangGraph involvement. Distinct from `attachments.image_processing
.normalize_image` (which exists purely to prep an image for a vision-model
prompt) and from `attachments.vision`/`attachments.ocr_extract` (which both
require running a model or OCR engine) -- this module is capability (C) in
CLAUDE.md's "four capabilities" split: pure, local, non-generative image
processing.
"""

from __future__ import annotations

import io
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from attachments.image_processing import ImageDecodeError
from config.settings import Settings

FitMode = Literal["contain", "cover", "stretch"]
OutputFormat = Literal["png", "jpeg", "webp"]

_FORMAT_TO_PIL: dict[OutputFormat, str] = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}
_PIL_TO_MEDIA_TYPE = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}

# Named presets the frontend's resize dialog offers directly -- also
# returned via GET /attachments/capabilities so the UI never has to
# hardcode a second copy of these numbers.
RESIZE_PRESETS: dict[str, tuple[int, int]] = {
    "thumbnail_128": (128, 128),
    "small_480": (480, 480),
    "medium_800": (800, 800),
    "large_1600": (1600, 1600),
    "social_1200x630": (1200, 630),
    "avatar_square_512": (512, 512),
}


class ImageResizeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    image_bytes: bytes
    media_type: str
    output_format: OutputFormat
    width: int
    height: int
    original_width: int
    original_height: int
    original_size_bytes: int
    size_bytes: int = Field(description="Byte size of the resized output.")


def _target_dimensions(
    orig_w: int, orig_h: int, width: int | None, height: int | None, fit: FitMode
) -> tuple[int, int]:
    if fit == "stretch":
        return (width or orig_w, height or orig_h)
    if width and height:
        ratio = (
            max(width / orig_w, height / orig_h)
            if fit == "cover"
            else min(width / orig_w, height / orig_h)
        )
        return max(1, round(orig_w * ratio)), max(1, round(orig_h * ratio))
    if width:
        ratio = width / orig_w
        return width, max(1, round(orig_h * ratio))
    ratio = (height or orig_h) / orig_h
    return max(1, round(orig_w * ratio)), height or orig_h


def _resize_and_crop(
    image: Image.Image, width: int | None, height: int | None, fit: FitMode
) -> Image.Image:
    orig_w, orig_h = image.size
    if width and height and fit == "cover":
        ratio = max(width / orig_w, height / orig_h)
        scaled_w, scaled_h = max(1, round(orig_w * ratio)), max(1, round(orig_h * ratio))
        scaled = image.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
        left = (scaled_w - width) // 2
        top = (scaled_h - height) // 2
        return scaled.crop((left, top, left + width, top + height))
    target_w, target_h = _target_dimensions(orig_w, orig_h, width, height, fit)
    return image.resize((target_w, target_h), Image.Resampling.LANCZOS)


def resize_image(
    file_bytes: bytes,
    settings: Settings,
    *,
    width: int | None = None,
    height: int | None = None,
    fit: FitMode = "contain",
    output_format: OutputFormat | None = None,
    quality: int = 90,
) -> ImageResizeResult:
    """Resizes `file_bytes` and returns the new image bytes plus before/after
    dimensions -- never touches a model, purely deterministic Pillow work.

    Args:
        width, height: Target box in pixels. At least one is required. When
            both are given, `fit` decides how the aspect ratio is handled:
            "contain" (default) scales to fit inside the box, possibly
            leaving one dimension smaller than requested; "cover" scales to
            fill the box and center-crops the overflow; "stretch" ignores
            aspect ratio entirely and matches the box exactly.
        output_format: "png"/"jpeg"/"webp", or `None` to keep the source
            format (falling back to PNG for an image with transparency, JPEG
            otherwise, if the source format isn't one of the three).
        quality: JPEG/WEBP quality (1-100); ignored for PNG.

    Raises:
        ImageDecodeError: `file_bytes` isn't a valid, decodable image.
        ValueError: no target dimension given, a dimension isn't positive, or
            a requested dimension exceeds
            `Settings.max_attachment_resize_dimension_px` (a decompression-
            bomb-style guard, not an arbitrary UX limit).
    """
    if width is None and height is None:
        raise ValueError("At least one of width or height is required.")
    max_dim = settings.max_attachment_resize_dimension_px
    for label, value in (("width", width), ("height", height)):
        if value is not None and value <= 0:
            raise ValueError(f"{label} must be positive.")
        if value is not None and value > max_dim:
            raise ValueError(f"{label} may not exceed {max_dim}px.")

    try:
        with Image.open(io.BytesIO(file_bytes)) as opened:
            source_format = (opened.format or "").upper()
            # exif_transpose applies the EXIF orientation tag and strips it,
            # so a photo taken sideways on a phone doesn't come out rotated
            # -- returns a new image, doesn't mutate `opened`. Original
            # dimensions are read *after* this, from the display-correct
            # (transposed) image -- reading them from `opened` directly
            # would report the raw, pre-rotation pixel grid (e.g. a portrait
            # phone photo stored as landscape pixels plus a rotation tag),
            # which doesn't match what the caller actually sees.
            transposed = ImageOps.exif_transpose(opened) or opened.copy()
            original_w, original_h = transposed.size
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageDecodeError(f"Could not decode image: {exc}") from exc

    has_alpha = transposed.mode in ("RGBA", "LA") or (
        transposed.mode == "P" and "transparency" in transposed.info
    )
    working = transposed.convert("RGBA" if has_alpha else "RGB")
    resized = _resize_and_crop(working, width, height, fit)

    if output_format is not None:
        resolved_format = output_format
    elif source_format in ("PNG", "JPEG", "WEBP"):
        resolved_format = source_format.lower()  # type: ignore[assignment]
    else:
        resolved_format = "png" if has_alpha else "jpeg"

    pil_format = _FORMAT_TO_PIL[resolved_format]
    buffer = io.BytesIO()
    if pil_format == "JPEG" and resized.mode == "RGBA":
        # JPEG has no alpha channel -- flatten onto white rather than
        # letting Pillow raise on save.
        flattened = Image.new("RGB", resized.size, (255, 255, 255))
        flattened.paste(resized, mask=resized.split()[3])
        resized = flattened
    save_kwargs = (
        {"quality": max(1, min(100, quality)), "optimize": True}
        if pil_format != "PNG"
        else {"optimize": True}
    )
    resized.save(buffer, format=pil_format, **save_kwargs)
    output_bytes = buffer.getvalue()

    return ImageResizeResult(
        image_bytes=output_bytes,
        media_type=_PIL_TO_MEDIA_TYPE[pil_format],
        output_format=resolved_format,
        width=resized.width,
        height=resized.height,
        original_width=original_w,
        original_height=original_h,
        original_size_bytes=len(file_bytes),
        size_bytes=len(output_bytes),
    )
