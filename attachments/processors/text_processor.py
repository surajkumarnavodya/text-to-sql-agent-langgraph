"""Processor for plain text and Markdown attachments."""

from __future__ import annotations

from pathlib import Path

from attachments.models import Attachment
from attachments.processors.base import ProcessingOutcome
from config.settings import Settings

_MEDIA_TYPES = frozenset({"text/plain", "text/markdown"})


def decode_text_bytes(file_bytes: bytes) -> tuple[str, str]:
    """Decodes `file_bytes` as text, without a third-party charset-detection
    dependency: tries UTF-8 (with and without a BOM) first -- the
    overwhelming common case for anything created or edited on a modern
    system -- and falls back to Latin-1 (which can decode any byte
    sequence, so this never raises) if that fails.

    Returns:
        `(text, encoding_used)` -- `encoding_used` is surfaced in
        `ProcessingOutcome.metadata` so a caller can tell whether the
        fallback path was taken.
    """
    try:
        return file_bytes.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return file_bytes.decode("latin-1"), "latin-1 (fallback -- not valid UTF-8)"


class TextProcessor:
    source_type = "text"

    def can_process(self, attachment: Attachment) -> bool:
        return attachment.media_type in _MEDIA_TYPES

    def process(self, attachment: Attachment, settings: Settings) -> ProcessingOutcome:
        file_bytes = Path(attachment.local_path).read_bytes()  # type: ignore[arg-type]
        text, encoding_used = decode_text_bytes(file_bytes)

        truncated = len(text) > settings.max_attachment_text_chars
        if truncated:
            text = text[: settings.max_attachment_text_chars]

        return ProcessingOutcome(
            status="succeeded",
            extracted_text=text,
            metadata={
                "encoding": encoding_used,
                "char_count": len(text),
                "line_count": text.count("\n") + 1,
                "truncated": truncated,
            },
        )
