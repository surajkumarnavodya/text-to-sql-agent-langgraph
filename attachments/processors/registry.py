"""Picks the right `FileProcessor` for a given attachment, by `media_type`.

A plain ordered list + first-match, not a `media_type -> processor` dict --
`TextProcessor` alone handles two media types (`text/plain`/`text/markdown`),
so a dict keyed by a single media type per entry doesn't fit every
processor uniformly; `can_process` is what actually owns that mapping,
kept next to each processor's own logic instead of duplicated here.
"""

from __future__ import annotations

from attachments.models import Attachment
from attachments.processors.base import FileProcessor
from attachments.processors.csv_processor import CsvProcessor
from attachments.processors.docx_processor import DocxProcessor
from attachments.processors.image_processor import ImageProcessor
from attachments.processors.json_processor import JsonProcessor
from attachments.processors.pdf_processor import PdfProcessor
from attachments.processors.pptx_processor import PptxProcessor
from attachments.processors.text_processor import TextProcessor
from attachments.processors.xlsx_processor import XlsxProcessor

_PROCESSORS: list[FileProcessor] = [
    ImageProcessor(),
    PdfProcessor(),
    DocxProcessor(),
    XlsxProcessor(),
    PptxProcessor(),
    JsonProcessor(),
    CsvProcessor(),
    TextProcessor(),
]


def get_processor_for(attachment: Attachment) -> FileProcessor | None:
    """Returns the first registered processor that can handle `attachment`,
    or `None` if nothing does (a media type that passed
    `attachments.validation` but has no processor -- shouldn't happen given
    `EXTENSION_TO_MEDIA_TYPE`'s allowlist matches this registry 1:1, but
    checked explicitly rather than assumed, per this feature's own "files
    that cannot be directly processed must be handled explicitly, never
    silently discarded" requirement)."""
    for processor in _PROCESSORS:
        if processor.can_process(attachment):
            return processor
    return None
