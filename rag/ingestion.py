"""PDF extraction -> chunking -> embedding -> storage pipeline.

The full "upload a PDF" path (`ui/pages/1_Knowledge_Sources.py` calls
`ingest_pdf` directly) -- extraction and chunking are pure functions
(`extract_pdf_pages`/`chunk_pages`), independently testable without a real
database or embedding model, and `ingest_pdf` wires them together with
`rag/store.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from io import BytesIO

from pypdf import PdfReader

from config.settings import Settings, get_settings
from rag.embedding import embed_texts
from rag.store import (
    Collection,
    SensitivityCategory,
    ensure_schema,
    get_rag_engine,
    insert_chunks,
    insert_document,
    update_document_status,
)

logger = logging.getLogger(__name__)

# Below this many extracted characters, a page is flagged as likely
# scanned/image-only rather than genuinely near-empty -- a real text page
# (even a sparse title page) almost always clears this; a scanned page with
# no OCR layer extracts to an empty or near-empty string via pypdf, which
# only reads embedded text, never rasterizes+OCRs an image.
_OCR_SUSPECT_CHAR_THRESHOLD = 20


@dataclass(frozen=True)
class ChunkDraft:
    """One chunk before embedding -- text plus enough position info to cite it."""

    text: str
    index: int
    page_number: int | None


@dataclass(frozen=True)
class IngestionResult:
    """Per-file outcome, shown in the Knowledge Sources upload UI."""

    document_id: str | None
    filename: str
    status: str  # "ready" | "failed"
    chunk_count: int
    warnings: list[str] = field(default_factory=list)
    error_message: str | None = None


def extract_pdf_pages(file_bytes: bytes) -> list[str]:
    """Extracts text per page from a PDF's raw bytes.

    Pure metadata/text extraction -- never rasterizes or OCRs. A page that
    extracts to (near-)nothing is very likely a scanned image page with no
    embedded text layer; `ingest_pdf` surfaces that as a warning rather than
    silently indexing an empty chunk (see `_OCR_SUSPECT_CHAR_THRESHOLD`).
    """
    reader = PdfReader(BytesIO(file_bytes))
    return [page.extract_text() or "" for page in reader.pages]


def chunk_pages(pages: list[str], chunk_size: int, overlap: int) -> list[ChunkDraft]:
    """Splits extracted page text into overlapping, page-tracked chunks.

    A simple character-based sliding window, not sentence/token-aware --
    deliberately: this is chunking already-extracted plain text for
    semantic retrieval, not something that needs to respect markdown/code
    structure the way, say, a source-file chunker would. Each page's text
    is joined with a form-feed-style boundary marker so a chunk's
    `page_number` is the page the chunk *starts* on -- approximate for a
    chunk spanning a page boundary, which is fine for citation purposes
    (points the reader to the right neighborhood, not a guaranteed exact
    page).

    Args:
        pages: Per-page extracted text, as returned by `extract_pdf_pages`.
        chunk_size: Target chunk length in characters (`Settings.rag_chunk_size`).
        overlap: Character overlap between consecutive chunks
            (`Settings.rag_chunk_overlap`) -- keeps a sentence spanning a
            chunk boundary from being lost to either chunk alone.

    Returns:
        Chunks in document order, empty/whitespace-only ones dropped.
    """
    # (start_char_offset, page_number) pairs -- used below to look up which
    # page a given character offset in the concatenated text falls on.
    boundaries: list[tuple[int, int]] = []
    parts: list[str] = []
    offset = 0
    for page_number, page_text in enumerate(pages, start=1):
        boundaries.append((offset, page_number))
        parts.append(page_text)
        offset += len(page_text) + 1  # +1 for the joining "\n" below
    full_text = "\n".join(parts)

    def _page_at(char_offset: int) -> int | None:
        page = None
        for start, number in boundaries:
            if start > char_offset:
                break
            page = number
        return page

    chunks: list[ChunkDraft] = []
    step = max(chunk_size - overlap, 1)
    index = 0
    start = 0
    while start < len(full_text):
        end = min(start + chunk_size, len(full_text))
        text = full_text[start:end].strip()
        if text:
            chunks.append(ChunkDraft(text=text, index=index, page_number=_page_at(start)))
            index += 1
        start += step
    return chunks


def ingest_pdf(
    file_bytes: bytes,
    filename: str,
    collection: Collection,
    sensitivity_category: SensitivityCategory = None,
    settings: Settings | None = None,
) -> IngestionResult:
    """Runs the full extract -> chunk -> embed -> store pipeline for one PDF.

    Always creates a `rag.documents` row (status "processing" immediately,
    then "ready"/"failed") before doing any real work, so a failure partway
    through still leaves a visible, inspectable record in the management
    view rather than silently vanishing -- consistent with this app's
    existing "fail visibly, never silently" posture (e.g. `AttemptRecord`
    in the SQL retry loop).
    """
    settings = settings or get_settings()
    engine = get_rag_engine(settings)
    ensure_schema(engine)

    document_id = insert_document(
        engine,
        filename,
        collection,
        sensitivity_category=sensitivity_category,
        pdf_bytes=file_bytes if settings.enable_pdf_download else None,
    )

    try:
        pages = extract_pdf_pages(file_bytes)
        warnings = [
            f"Page {i}: little to no extractable text -- may be a scanned image "
            "that needs OCR before it can be indexed."
            for i, page_text in enumerate(pages, start=1)
            if len(page_text.strip()) < _OCR_SUSPECT_CHAR_THRESHOLD
        ]

        drafts = chunk_pages(pages, settings.rag_chunk_size, settings.rag_chunk_overlap)
        if not drafts:
            update_document_status(
                engine,
                document_id,
                "failed",
                chunk_count=0,
                error_message="No extractable text found in this PDF (it may be scanned/image-only).",
            )
            return IngestionResult(
                document_id=document_id,
                filename=filename,
                status="failed",
                chunk_count=0,
                warnings=warnings,
                error_message="No extractable text found in this PDF (it may be scanned/image-only).",
            )

        embeddings = embed_texts([d.text for d in drafts], settings)
        insert_chunks(
            engine,
            document_id,
            [
                (draft.text, draft.index, embedding, draft.page_number)
                for draft, embedding in zip(drafts, embeddings, strict=True)
            ],
        )
        update_document_status(engine, document_id, "ready", chunk_count=len(drafts))
        logger.info(
            "[rag.ingestion] ingested filename=%r collection=%s chunks=%d warnings=%d",
            filename,
            collection,
            len(drafts),
            len(warnings),
        )
        return IngestionResult(
            document_id=document_id,
            filename=filename,
            status="ready",
            chunk_count=len(drafts),
            warnings=warnings,
        )
    except Exception as exc:  # noqa: BLE001 - must still mark the document failed either way
        logger.exception("[rag.ingestion] failed filename=%r collection=%s", filename, collection)
        update_document_status(engine, document_id, "failed", chunk_count=0, error_message=str(exc))
        return IngestionResult(
            document_id=document_id,
            filename=filename,
            status="failed",
            chunk_count=0,
            error_message=str(exc),
        )
