"""The common interface every attachment file processor implements, plus the
shared result shape they all return.

One processor class per file kind (`text_processor.py`, `json_processor.py`,
`csv_processor.py`, `pdf_processor.py`, `docx_processor.py`,
`xlsx_processor.py`, `pptx_processor.py`, `image_processor.py`) -- per this
feature's own requirement, deliberately not one large dispatch function.
`attachments.processors.registry` is what picks which one runs for a given
attachment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from attachments.models import Attachment, ProcessingStatus
from config.settings import Settings


@dataclass
class ProcessingOutcome:
    """A processor's raw result -- converted into both an updated
    `Attachment` (`attachments.pipeline.apply_processing_outcome`) and a
    `ProcessedAttachment` dict (`attachments.pipeline.to_processed_attachment`)
    by the pipeline layer, so every processor returns exactly one shape
    regardless of which of those two the caller ultimately needs.
    """

    status: ProcessingStatus
    extracted_text: str | None = None
    extracted_tables: list[dict] | None = None
    metadata: dict = field(default_factory=dict)
    image_data_url: str | None = None
    error: str | None = None


class FileProcessor(Protocol):
    """One file kind's validation-to-extraction logic."""

    #: A short, stable label for `ProcessedAttachment.source_type` (e.g.
    #: "pdf", "image", "docx") -- set as a plain class attribute by each
    #: implementation rather than computed, since it never varies per call.
    source_type: str

    def can_process(self, attachment: Attachment) -> bool:
        """Whether this processor handles `attachment.media_type`."""
        ...

    def process(self, attachment: Attachment, settings: Settings) -> ProcessingOutcome:
        """Reads `attachment.local_path` and extracts its content.

        Never raises for an ordinary bad-input case (corrupt file, invalid
        JSON, password-protected PDF, ...) -- those all come back as
        `ProcessingOutcome(status="failed", error=...)`, per this feature's
        "preserve the failure in state, don't crash the app" requirement.
        An unexpected exception (a genuine bug, a disk I/O error reading
        `local_path`) is allowed to propagate; `attachments.pipeline` is
        what catches that outer case and converts it to a generic failed
        outcome so a single processor bug can't crash the whole request.
        """
        ...
