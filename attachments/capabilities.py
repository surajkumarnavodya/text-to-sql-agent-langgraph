"""A single, honest capability registry for every attachment-related
operation -- `GET /attachments/capabilities` (`api/attachments.py`) returns
this so the frontend can enable/disable each action truthfully instead of
guessing or assuming every operation is always available. Computed fresh
from live `Settings` on every call (cheap -- no network/model probe), never
a hardcoded dict, since whether vision/OCR/editing are actually usable
depends entirely on what's configured (a vision model pulled, Tesseract
installed, chat attachments enabled at all).

Distinguishes the four capabilities this feature is built around (see
CLAUDE.md's "Chat attachments" section and this package's own module
docstrings): image understanding (a vision-capable model), OCR (Tesseract,
independent of any model), deterministic image manipulation (Pillow resize,
always available once chat attachments are enabled -- no external
dependency), and image editing / text removal (OpenCV classical inpainting,
distinguished explicitly from a generative-AI backend, which this
deployment does not have).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from attachments.image_ops import RESIZE_PRESETS
from attachments.validation import DOCUMENT_MEDIA_TYPES, EXTENSION_TO_MEDIA_TYPE, IMAGE_MEDIA_TYPES
from config.settings import Settings


class ImageResizePresetOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    width: int
    height: int


class AttachmentCapabilitiesOut(BaseModel):
    """The spec's own `MODEL_CAPABILITIES`-shaped response, extended with
    the other three capabilities this feature also exposes."""

    model_config = ConfigDict(frozen=True)

    enabled: bool
    vision_input: bool
    vision_model: str | None = None
    ocr: bool
    image_resize: bool
    image_text_removal: bool
    image_text_removal_method: str | None = None
    native_pdf_input: bool = False
    max_image_bytes: int
    max_document_bytes: int
    max_attachments_per_message: int
    max_total_attachment_bytes: int
    max_resize_dimension_px: int
    max_text_removal_regions: int
    supported_image_extensions: list[str] = Field(default_factory=list)
    supported_document_extensions: list[str] = Field(default_factory=list)
    resize_presets: list[ImageResizePresetOut] = Field(default_factory=list)


def get_attachment_capabilities(settings: Settings) -> AttachmentCapabilitiesOut:
    """Whether Tesseract is actually *installed* (a system binary, not a
    pip package -- see CLAUDE.md's Windows-specific notes) can't be checked
    cheaply here without spending a real OCR call; `ocr`/`image_text_removal`
    are reported `True` whenever `pytesseract` is importable, matching the
    same fail-open-at-call-time posture `attachments.ocr_extract`/
    `attachments.inpaint` already have (a missing binary degrades a single
    request to an honest warning, not a capability flag flip)."""
    try:
        import pytesseract  # noqa: F401

        ocr_importable = True
    except ImportError:
        ocr_importable = False

    vision_model = settings.media_vision_model or None
    return AttachmentCapabilitiesOut(
        enabled=settings.enable_chat_attachments,
        vision_input=bool(vision_model),
        vision_model=vision_model,
        ocr=ocr_importable,
        image_resize=settings.enable_chat_attachments,
        image_text_removal=ocr_importable,
        image_text_removal_method="opencv_telea_inpaint" if ocr_importable else None,
        native_pdf_input=False,
        max_image_bytes=settings.max_attachment_image_bytes,
        max_document_bytes=settings.max_attachment_document_bytes,
        max_attachments_per_message=settings.max_attachments_per_message,
        max_total_attachment_bytes=settings.max_total_attachment_bytes,
        max_resize_dimension_px=settings.max_attachment_resize_dimension_px,
        max_text_removal_regions=settings.max_text_removal_regions,
        supported_image_extensions=sorted(
            ext
            for ext, media_type in EXTENSION_TO_MEDIA_TYPE.items()
            if media_type in IMAGE_MEDIA_TYPES
        ),
        supported_document_extensions=sorted(
            ext
            for ext, media_type in EXTENSION_TO_MEDIA_TYPE.items()
            if media_type in DOCUMENT_MEDIA_TYPES
        ),
        resize_presets=[
            ImageResizePresetOut(name=name, width=w, height=h)
            for name, (w, h) in RESIZE_PRESETS.items()
        ],
    )


__all__ = ["AttachmentCapabilitiesOut", "ImageResizePresetOut", "get_attachment_capabilities"]
