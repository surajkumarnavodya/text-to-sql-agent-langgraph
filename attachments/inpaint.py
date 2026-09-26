"""Text removal / image editing -- capability (D) in CLAUDE.md's "four
capabilities" split, and deliberately the most honestly-labeled one.

OCR (capability B, `attachments.ocr_extract`) only *reads* text; it never
edits pixels. This module is what actually removes them: it builds a binary
mask over the text region(s) (either OCR-detected line boxes the caller
confirmed, or a manually drawn selection) and reconstructs the background
underneath using OpenCV's classical inpainting (`cv2.inpaint`, Telea's
fast-marching-method algorithm) -- a real, deterministic image-editing
algorithm, not a solid rectangle pasted over the text and not a generative
diffusion/GAN model. That distinction is surfaced to the caller explicitly
(`TextRemovalResult.warnings`) rather than implied away: on a complex,
highly-textured background the reconstruction can look blurred or show
visible artifacts, which a generative inpainting model would usually avoid.
If a genuinely generative inpainting backend is ever wired in, it belongs
here as a second method behind the same function signature -- not as a
silent behavior change to what "Remove text" already means today.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from attachments.image_processing import ImageDecodeError
from attachments.ocr_extract import downscale_if_oversized
from config.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class _LineAccumulator:
    """Plain, precisely-typed running total for one OCR text line being
    merged from several per-word boxes -- see `detect_text_line_regions`
    below. A typed dataclass rather than a loosely-typed dict of mixed
    value types, which mypy can't meaningfully check field-by-field."""

    words: list[str]
    left: int
    top: int
    right: int
    bottom: int
    confidences: list[float] = field(default_factory=list)


InpaintMethod = Literal["opencv_telea_inpaint"]

_HONEST_LIMITATION_WARNING = (
    "Text removal uses classical inpainting (OpenCV's Telea algorithm), not "
    "a generative AI model -- it reconstructs the background from nearby "
    "pixels. Results on complex, highly-textured, or high-contrast "
    "backgrounds may show blurring or visible artifacts."
)


class TextLineRegion(BaseModel):
    """One OCR-detected line of text, merged from Tesseract's own per-word
    boxes -- coarser than `attachments.ocr_extract.TextRegion` (which is
    per-word) since a per-line box is what a text-removal mask actually
    wants: fewer, larger regions with less jagged mask geometry than
    unioning many small per-word rectangles would produce."""

    model_config = ConfigDict(frozen=True)

    region_id: int
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float


class InpaintRegion(BaseModel):
    """One axis-aligned rectangle, in source-image pixel coordinates --
    either copied from a `TextLineRegion` the caller confirmed, or drawn
    freehand by the user over the image preview."""

    model_config = ConfigDict(frozen=True)

    left: int = Field(ge=0)
    top: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class TextRemovalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    image_bytes: bytes
    media_type: str
    width: int
    height: int
    regions_removed: int
    method: InpaintMethod
    warnings: list[str] = Field(default_factory=list)


def detect_text_line_regions(file_bytes: bytes, settings: Settings) -> list[TextLineRegion]:
    """OCR-detects text and merges Tesseract's per-word boxes into per-line
    bounding boxes -- the proposed-region list `POST
    /attachments/{id}/detect-text-regions` returns for the "Remove text"
    workflow's confirmation step. Capped at
    `Settings.max_text_removal_regions` (largest-area first) so a dense,
    text-heavy image can't request an unbounded inpainting mask.

    Fails open (returns `[]`) on a missing Tesseract binary or OCR engine
    error, matching every other OCR call site in this codebase.
    """
    import pytesseract
    from pytesseract import Output

    try:
        with Image.open(io.BytesIO(file_bytes)) as opened:
            transposed = ImageOps.exif_transpose(opened) or opened.copy()
    except Exception as exc:  # noqa: BLE001 - any decode failure means "not a real image"
        raise ImageDecodeError(f"Could not decode image: {exc}") from exc

    working, scale = downscale_if_oversized(
        transposed.convert("RGB"), settings.max_attachment_resize_dimension_px
    )

    try:
        data = pytesseract.image_to_data(
            working, output_type=Output.DICT, timeout=settings.attachment_ocr_timeout_seconds
        )
    except Exception:
        logger.warning("[attachments.inpaint] OCR unavailable for region detection", exc_info=True)
        return []

    inverse_scale = 1.0 / scale if scale else 1.0
    accumulators: dict[tuple[int, int, int], _LineAccumulator] = {}
    for index, text in enumerate(data.get("text", [])):
        stripped = text.strip()
        confidence = float(data["conf"][index])
        if not stripped or confidence < 0:
            continue
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        left, top = data["left"][index], data["top"][index]
        right, bottom = left + data["width"][index], top + data["height"][index]
        line = accumulators.get(key)
        if line is None:
            accumulators[key] = _LineAccumulator(
                words=[stripped],
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                confidences=[confidence],
            )
        else:
            line.words.append(stripped)
            line.left = min(line.left, left)
            line.top = min(line.top, top)
            line.right = max(line.right, right)
            line.bottom = max(line.bottom, bottom)
            line.confidences.append(confidence)

    regions = [
        TextLineRegion(
            region_id=region_id,
            text=" ".join(line.words),
            left=round(line.left * inverse_scale),
            top=round(line.top * inverse_scale),
            width=round((line.right - line.left) * inverse_scale),
            height=round((line.bottom - line.top) * inverse_scale),
            confidence=sum(line.confidences) / len(line.confidences),
        )
        for region_id, line in enumerate(accumulators.values())
    ]
    regions.sort(key=lambda region: region.width * region.height, reverse=True)
    return regions[: settings.max_text_removal_regions]


def remove_text(
    file_bytes: bytes,
    settings: Settings,
    *,
    regions: list[InpaintRegion],
    dilate_px: int = 4,
) -> TextRemovalResult:
    """Removes text from `regions` via OpenCV inpainting and returns the
    edited image -- see this module's own docstring for exactly what
    algorithm this is and its honest limitations.

    Args:
        regions: Rectangles to mask and reconstruct, in source-image pixel
            coordinates. Either OCR-proposed (`detect_text_line_regions`,
            caller-confirmed) or manually drawn. Required, non-empty.
        dilate_px: How many pixels to expand each region's mask by on every
            side -- text edges (especially anti-aliased font rendering)
            extend slightly past a tight OCR/selection box; a small dilation
            avoids a visible text-colored fringe surviving at the mask edge.

    Raises:
        ImageDecodeError: `file_bytes` isn't a valid, decodable image.
        ValueError: no regions given, or more than
            `Settings.max_text_removal_regions`.
    """
    import cv2

    if not regions:
        raise ValueError("At least one region is required.")
    if len(regions) > settings.max_text_removal_regions:
        raise ValueError(
            f"At most {settings.max_text_removal_regions} regions are allowed per request."
        )

    try:
        with Image.open(io.BytesIO(file_bytes)) as opened:
            transposed = ImageOps.exif_transpose(opened) or opened.copy()
            image_rgb = transposed.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - any decode failure means "not a real image"
        raise ImageDecodeError(f"Could not decode image: {exc}") from exc

    width, height = image_rgb.size
    array_rgb = np.array(image_rgb)
    array_bgr = array_rgb[:, :, ::-1].copy()

    mask = np.zeros((height, width), dtype=np.uint8)
    for region in regions:
        left = max(0, region.left - dilate_px)
        top = max(0, region.top - dilate_px)
        right = min(width, region.left + region.width + dilate_px)
        bottom = min(height, region.top + region.height + dilate_px)
        if right > left and bottom > top:
            mask[top:bottom, left:right] = 255

    inpainted_bgr = cv2.inpaint(array_bgr, mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)
    inpainted_rgb = inpainted_bgr[:, :, ::-1]

    buffer = io.BytesIO()
    Image.fromarray(inpainted_rgb).save(buffer, format="PNG", optimize=True)
    output_bytes = buffer.getvalue()

    return TextRemovalResult(
        image_bytes=output_bytes,
        media_type="image/png",
        width=width,
        height=height,
        regions_removed=len(regions),
        method="opencv_telea_inpaint",
        warnings=[_HONEST_LIMITATION_WARNING],
    )


__all__ = [
    "InpaintRegion",
    "TextLineRegion",
    "TextRemovalResult",
    "detect_text_line_regions",
    "remove_text",
]
