"""Processor for PDF attachments.

Reuses `rag.ingestion.extract_pdf_pages` for the actual text extraction --
the exact same pure function the persistent Knowledge Sources pipeline uses
-- rather than a second implementation. Deliberately does NOT reuse the
rest of `rag/ingestion.py::ingest_pdf` (moderation gate, malware scan,
vector embedding, SQL Server storage): that pipeline is for a permanent,
shared knowledge base ingested once and retrieved many times later; a chat
attachment is ephemeral, per-conversation, per-caller content given
directly to the model as context for the question that attached it, with
no retrieval step of its own. See `attachments/store.py`'s docstring for
the storage-model difference this reflects.

Password-protected PDF handling: `pypdf` flags a PDF encrypted even when it
only carries an *empty* user password (common for PDFs exported by some
tools with restrictions but no real access password) -- this processor
tries decrypting with an empty password first, and only reports
`PASSWORD_PROTECTED` if that fails, so those common false-positive cases
still extract normally.

Scanned-page OCR fallback mirrors `rag/ingestion.py::_ocr_suspect_pages`'s
own rasterize-via-pymupdf-then-`media.ocr.extract_text` pattern (a small,
deliberate duplication rather than importing that module's private helper
-- see this module's own docstring above for why these are two separate
pipelines).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pymupdf
from pypdf.errors import PdfReadError

from attachments.models import Attachment
from attachments.pdf_safety import PdfSafetyError, check_pdf_safety
from attachments.processors.base import ProcessingOutcome
from config.settings import Settings
from media.ocr import extract_text
from rag.ingestion import extract_pdf_pages

logger = logging.getLogger(__name__)

# Mirrors rag/ingestion.py's _OCR_SUSPECT_CHAR_THRESHOLD -- a real text page
# (even a sparse title page) almost always clears this; a scanned page with
# no OCR layer extracts to an empty or near-empty string via pypdf.
_OCR_SUSPECT_CHAR_THRESHOLD = 20


def _ocr_suspect_pages(file_bytes: bytes, pages: list[str]) -> dict[int, str]:
    suspect_page_numbers = [
        i
        for i, page_text in enumerate(pages, start=1)
        if len(page_text.strip()) < _OCR_SUSPECT_CHAR_THRESHOLD
    ]
    if not suspect_page_numbers:
        return {}

    ocr_text_by_page: dict[int, str] = {}
    doc = pymupdf.open(stream=file_bytes, filetype="pdf")
    try:
        for page_number in suspect_page_numbers:
            tmp_path: Path | None = None
            try:
                pixmap = doc[page_number - 1].get_pixmap()
                fd, tmp_path_str = tempfile.mkstemp(suffix=".png", prefix="attachment_pdf_page_")
                os.close(fd)
                tmp_path = Path(tmp_path_str)
                pixmap.save(tmp_path)
                ocr_text_by_page[page_number] = extract_text(tmp_path)
            except Exception:  # noqa: BLE001 - one bad page must not abort the whole document
                logger.warning(
                    "[attachments.pdf] failed to rasterize/OCR page %d",
                    page_number,
                    exc_info=True,
                )
                ocr_text_by_page[page_number] = ""
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
    finally:
        doc.close()
    return ocr_text_by_page


class PdfProcessor:
    source_type = "pdf"

    def can_process(self, attachment: Attachment) -> bool:
        return attachment.media_type == "application/pdf"

    def process(self, attachment: Attachment, settings: Settings) -> ProcessingOutcome:
        file_bytes = Path(attachment.local_path).read_bytes()  # type: ignore[arg-type]

        from io import BytesIO

        from pypdf import PdfReader

        try:
            probe = PdfReader(BytesIO(file_bytes))
        except PdfReadError as exc:
            return ProcessingOutcome(status="failed", error=f"Could not read this PDF: {exc}.")

        if probe.is_encrypted:
            decrypt_failed = False
            try:
                probe.decrypt("")
            except Exception:  # noqa: BLE001 - any decrypt failure means it's genuinely protected
                decrypt_failed = True
            if decrypt_failed or probe.is_encrypted:
                return ProcessingOutcome(
                    status="failed",
                    error=(
                        "This PDF is password-protected. Please remove the password "
                        "and upload it again."
                    ),
                )

        try:
            check_pdf_safety(probe)
        except PdfSafetyError as exc:
            return ProcessingOutcome(status="failed", error=str(exc))

        try:
            pages = extract_pdf_pages(file_bytes, max_pages=settings.max_attachment_document_pages)
        except ValueError as exc:
            return ProcessingOutcome(status="failed", error=str(exc))
        except PdfReadError as exc:
            return ProcessingOutcome(status="failed", error=f"Could not read this PDF: {exc}.")

        scanned_page_numbers = [
            i
            for i, page_text in enumerate(pages, start=1)
            if len(page_text.strip()) < _OCR_SUSPECT_CHAR_THRESHOLD
        ]
        ocr_text_by_page = _ocr_suspect_pages(file_bytes, pages) if scanned_page_numbers else {}

        chunks: list[dict] = []
        rendered_pages: list[str] = []
        for page_number, page_text in enumerate(pages, start=1):
            resolved_text = (
                page_text if page_text.strip() else ocr_text_by_page.get(page_number, "")
            )
            chunks.append(
                {
                    "attachment_id": attachment.attachment_id,
                    "filename": attachment.original_filename,
                    "page_number": page_number,
                    "source_type": "pdf",
                    "chunk_index": page_number - 1,
                    "text": resolved_text,
                }
            )
            if resolved_text.strip():
                rendered_pages.append(f"[Page {page_number}]\n{resolved_text.strip()}")

        combined_text = "\n\n".join(rendered_pages)
        truncated = len(combined_text) > settings.max_attachment_text_chars
        if truncated:
            combined_text = combined_text[: settings.max_attachment_text_chars]

        if not combined_text.strip():
            return ProcessingOutcome(
                status="failed",
                error=(
                    "No extractable text was found in this PDF (it may be a scanned "
                    "image with no OCR text available)."
                ),
                metadata={"page_count": len(pages)},
            )

        return ProcessingOutcome(
            status="succeeded",
            extracted_text=combined_text,
            metadata={
                "page_count": len(pages),
                "scanned_pages_ocrd": sorted(
                    p for p in scanned_page_numbers if ocr_text_by_page.get(p, "").strip()
                ),
                "truncated": truncated,
            },
        )
