"""OCR / text extraction -- capability (B) in CLAUDE.md's "four
capabilities" split, deliberately separate from image *understanding*
(`attachments.vision`, capability A). "What does this image show?" and
"Extract all visible text" are different operations answered by different
subsystems: the former asks a vision-capable LLM to describe/reason about
the image, the latter runs a real OCR engine (Tesseract, via `pytesseract`)
and returns exactly the characters it recognized -- never an LLM paraphrase
of them. Reuses the same Tesseract dependency `media/ocr.py` already
requires (see that module's own docstring for the system-binary install
note), but returns structured regions/confidence rather than a bare string,
per this feature's own "Extract text" action requirements.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from attachments.image_processing import ImageDecodeError
from config.settings import Settings

logger = logging.getLogger(__name__)

# Below this Tesseract word-confidence score (0-100), a recognized word is
# flagged as uncertain in `TextRegion.low_confidence` rather than silently
# presented with the same trust level as a clean read.
_LOW_CONFIDENCE_THRESHOLD = 60


class TextRegion(BaseModel):
    """One recognized word/token and its bounding box, in source-image
    pixel coordinates -- Tesseract's own per-word granularity
    (`image_to_data`'s `level=5` rows), not per-line, since a per-word box
    is what `attachments.inpaint`'s manual-region-confirmation UI needs to
    let a user toggle individual words on/off."""

    model_config = ConfigDict(frozen=True)

    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float
    low_confidence: bool


class OcrExtractionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw_text: str = Field(description="Tesseract's own line-preserving output, unmodified.")
    cleaned_text: str = Field(
        description="raw_text with collapsed blank lines/trailing whitespace -- still exact "
        "OCR output, never an LLM rewrite."
    )
    regions: list[TextRegion] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def downscale_if_oversized(image: Image.Image, max_dim: int) -> tuple[Image.Image, float]:
    """Returns `(image, scale)` -- `image` downscaled (aspect-preserving) if
    either dimension exceeds `max_dim`, and `scale` (<=1.0) to map detected
    coordinates back to the *original* image's pixel space, since an
    attachment's on-disk bytes are never pre-downscaled the way the
    vision-model data-url path is (see `Settings
    .max_attachment_resize_dimension_px`'s own docstring)."""
    width, height = image.size
    if width <= max_dim and height <= max_dim:
        return image, 1.0
    scale = min(max_dim / width, max_dim / height)
    resized = image.resize(
        (max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS
    )
    return resized, scale


def _clean_text(raw_text: str) -> str:
    lines = [line.rstrip() for line in raw_text.splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def extract_text_with_regions(file_bytes: bytes, settings: Settings) -> OcrExtractionResult:
    """Runs Tesseract OCR on an image and returns raw + cleaned text plus
    per-word bounding boxes and confidence scores.

    Fails open on a missing Tesseract binary or a genuine OCR engine error
    (returns an empty result with a warning, matching `media.ocr.extract_text`'s
    own "never block on OCR" posture) -- but a *decode* failure (not a real
    image at all) still raises `ImageDecodeError`, since that's a caller
    input error, not an OCR-availability concern.
    """
    import pytesseract
    from pytesseract import Output

    try:
        with Image.open(io.BytesIO(file_bytes)) as opened:
            transposed = ImageOps.exif_transpose(opened) or opened.copy()
    except Exception as exc:  # noqa: BLE001 - genuinely any decode failure means "not a real image"
        raise ImageDecodeError(f"Could not decode image: {exc}") from exc

    working, scale = downscale_if_oversized(
        transposed.convert("RGB"), settings.max_attachment_resize_dimension_px
    )

    try:
        raw_text = pytesseract.image_to_string(
            working, timeout=settings.attachment_ocr_timeout_seconds
        ).strip()
        data = pytesseract.image_to_data(
            working, output_type=Output.DICT, timeout=settings.attachment_ocr_timeout_seconds
        )
    except RuntimeError:
        logger.warning("[attachments.ocr_extract] OCR timed out")
        return OcrExtractionResult(
            raw_text="", cleaned_text="", regions=[], warnings=["OCR timed out."]
        )
    except Exception:
        logger.warning("[attachments.ocr_extract] OCR unavailable", exc_info=True)
        return OcrExtractionResult(
            raw_text="",
            cleaned_text="",
            regions=[],
            warnings=["OCR is not available on this server."],
        )

    inverse_scale = 1.0 / scale if scale else 1.0
    regions: list[TextRegion] = []
    for index, text in enumerate(data.get("text", [])):
        stripped = text.strip()
        if not stripped:
            continue
        confidence = float(data["conf"][index])
        if confidence < 0:  # Tesseract uses -1 for non-word rows (block/line-level entries)
            continue
        regions.append(
            TextRegion(
                text=stripped,
                left=round(data["left"][index] * inverse_scale),
                top=round(data["top"][index] * inverse_scale),
                width=round(data["width"][index] * inverse_scale),
                height=round(data["height"][index] * inverse_scale),
                confidence=confidence,
                low_confidence=confidence < _LOW_CONFIDENCE_THRESHOLD,
            )
        )

    warnings: list[str] = []
    if not raw_text and not regions:
        warnings.append("No text was detected in this image.")
    elif any(region.low_confidence for region in regions):
        warnings.append("Some recognized text has low confidence and may be inaccurate.")

    return OcrExtractionResult(
        raw_text=raw_text,
        cleaned_text=_clean_text(raw_text),
        regions=regions,
        warnings=warnings,
    )


__all__ = [
    "OcrExtractionResult",
    "TextRegion",
    "downscale_if_oversized",
    "extract_text_with_regions",
]
