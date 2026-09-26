"""Processor for image attachments -- normalizes and encodes the image as a
base64 data URL (`attachments.image_processing`). Never produces
`extracted_text` itself: describing *what's in* the image needs the user's
question too (which processor doesn't have -- attachments are processed
once, independent of any specific question), so that happens later, in
`attachments.graph`'s `call_model` node, via `attachments.vision.describe_images`."""

from __future__ import annotations

from pathlib import Path

from attachments.image_processing import ImageDecodeError, image_bytes_to_data_url, normalize_image
from attachments.models import Attachment
from attachments.processors.base import ProcessingOutcome
from attachments.validation import IMAGE_MEDIA_TYPES
from config.settings import Settings


class ImageProcessor:
    source_type = "image"

    def can_process(self, attachment: Attachment) -> bool:
        return attachment.media_type in IMAGE_MEDIA_TYPES

    def process(self, attachment: Attachment, settings: Settings) -> ProcessingOutcome:
        file_bytes = Path(attachment.local_path).read_bytes()  # type: ignore[arg-type]
        try:
            normalized_bytes, media_type = normalize_image(file_bytes, settings)
        except ImageDecodeError as exc:
            return ProcessingOutcome(
                status="failed",
                error=f"This image appears to be corrupted or invalid: {exc}.",
            )

        data_url = image_bytes_to_data_url(normalized_bytes, media_type)
        return ProcessingOutcome(
            status="succeeded",
            image_data_url=data_url,
            metadata={
                "normalized_media_type": media_type,
                "normalized_size_bytes": len(normalized_bytes),
            },
        )
