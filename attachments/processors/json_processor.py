"""Processor for JSON attachments -- parses and pretty-prints, rejecting
invalid JSON with a useful error rather than passing raw (possibly
minified/unreadable) bytes through as "extracted text"."""

from __future__ import annotations

import json
from pathlib import Path

from attachments.models import Attachment
from attachments.processors.base import ProcessingOutcome
from attachments.processors.text_processor import decode_text_bytes
from config.settings import Settings


class JsonProcessor:
    source_type = "json"

    def can_process(self, attachment: Attachment) -> bool:
        return attachment.media_type == "application/json"

    def process(self, attachment: Attachment, settings: Settings) -> ProcessingOutcome:
        file_bytes = Path(attachment.local_path).read_bytes()  # type: ignore[arg-type]
        text, _encoding = decode_text_bytes(file_bytes)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return ProcessingOutcome(
                status="failed",
                error=f"Invalid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}.",
            )

        pretty = json.dumps(parsed, indent=2, ensure_ascii=False, default=str)
        truncated = len(pretty) > settings.max_attachment_text_chars
        if truncated:
            pretty = pretty[: settings.max_attachment_text_chars]

        top_level_type = type(parsed).__name__
        return ProcessingOutcome(
            status="succeeded",
            extracted_text=pretty,
            metadata={
                "valid_json": True,
                "top_level_type": top_level_type,
                "item_count": len(parsed) if isinstance(parsed, list | dict) else None,
                "truncated": truncated,
            },
        )
