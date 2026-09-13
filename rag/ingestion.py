"""PDF extraction -> chunking -> moderation -> embedding -> storage pipeline.

The full "upload a PDF" path (`api/documents.py`'s upload route calls
`ingest_pdf` directly) -- extraction and chunking are pure functions
(`extract_pdf_pages`/`chunk_pages`), independently testable without a real
database or embedding model, and `ingest_pdf` wires them together with
`rag/store.py` and the pre-ingestion moderation gate (`moderation/gate.py`).

**Moderation gate (mandatory, not a feature flag -- see
`config/settings.py`'s `moderation_provider` docstring).** Before this
module ever calls `rag/store.py::insert_document` (which can itself
persist the file's raw bytes, if `Settings.enable_pdf_download` is on),
`moderation.store.get_asset_by_hash` checks whether this exact content has
already been moderated -- a prior "passed" or "rejected" result
short-circuits before any extraction/embedding work. For genuinely new
content, every page's extracted text, every page flagged as likely-scanned
(rasterized via `pymupdf` and OCR'd via the existing
`media.ocr.extract_text`, reused as-is), and every embedded image
(extracted via `pypdf`'s `page.images`) is checked by
`moderation.gate.moderate_chunks` *before* `insert_document` is ever
called. A hard-reject on any chunk means the file is rejected in full:
`rag.documents`/`rag.chunks` never gain a row for it, and
`moderation.store.record_asset` records only the hash/category/timestamp,
never the content -- closing a real gap the pre-moderation flow had
(`insert_document` used to run *before* any content check, which could
already persist `pdf_bytes` ahead of a rejection being known).

**Known, narrow limitation:** the dedupe short-circuit is keyed purely on
file content hash, not `(hash, collection, sensitivity_category)`. Re-
uploading byte-identical PDF content to a *different* collection or with a
different sensitivity tag than its first upload reuses the first upload's
existing `rag.documents` row (collection/tag included) rather than creating
a second one for the new intent -- a real, deliberately accepted tradeoff
for the common case (skip redundant moderation/embedding entirely for a
true duplicate), not silently mishandled.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import pymupdf
from pypdf import PdfReader

from config.settings import Settings, get_settings
from media.ocr import extract_text
from moderation.gate import decision_summary, moderate_chunks
from moderation.store import (
    ensure_schema as ensure_moderation_schema,
    get_asset_by_hash,
    get_moderation_engine,
    record_asset,
)
from moderation.types import ModerationChunk
from rag.embedding import embed_texts
from rag.store import (
    Collection,
    SensitivityCategory,
    ensure_schema as ensure_rag_schema,
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


def _content_hash(file_bytes: bytes) -> str:
    """SHA-256 of the file's bytes -- the same dedupe key
    `media/ingest.py::_content_hash` uses for images/video, applied here to
    PDF bytes instead of a file on disk."""
    return hashlib.sha256(file_bytes).hexdigest()


def extract_pdf_pages(file_bytes: bytes) -> list[str]:
    """Extracts text per page from a PDF's raw bytes.

    Pure metadata/text extraction -- never rasterizes or OCRs. A page that
    extracts to (near-)nothing is very likely a scanned image page with no
    embedded text layer; `ingest_pdf` OCRs it separately (see
    `_ocr_suspect_pages`) rather than silently indexing an empty chunk (see
    `_OCR_SUSPECT_CHAR_THRESHOLD`).
    """
    reader = PdfReader(BytesIO(file_bytes))
    return [page.extract_text() or "" for page in reader.pages]


def _ocr_suspect_pages(file_bytes: bytes, pages: list[str]) -> dict[int, str]:
    """OCRs every page whose extracted text looks like it's scanned/
    image-only -- rasterizes each such page to a temp PNG via `pymupdf`,
    then reuses `media.ocr.extract_text` (Tesseract, fails open) exactly as
    `media/ingest.py` does for video keyframes.

    Returns:
        1-indexed page number -> OCR'd text, for every suspect page. A page
        not in this dict either had enough embedded text already, or OCR
        found nothing (fails open to "" per `extract_text`'s own contract,
        which still shows up here as an empty string, not a missing key).
    """
    suspect_page_numbers = [
        i for i, page_text in enumerate(pages, start=1) if len(page_text.strip()) < _OCR_SUSPECT_CHAR_THRESHOLD
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
                fd, tmp_path_str = tempfile.mkstemp(suffix=".png", prefix="pdf_page_")
                os.close(fd)  # the fd must be closed before pymupdf can write to this path (Windows)
                tmp_path = Path(tmp_path_str)
                pixmap.save(tmp_path)
                ocr_text_by_page[page_number] = extract_text(tmp_path)
            except Exception:  # noqa: BLE001 - one bad page must not abort the whole document
                logger.warning(
                    "[rag.ingestion] failed to rasterize/OCR page %d, indexing without it",
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


def _extract_embedded_images(file_bytes: bytes) -> dict[int, list[Path]]:
    """Extracts every embedded image on every page to temp files, for
    moderation as separate image chunks (`ingest_pdf`) -- per this
    feature's requirement to chunk "per embedded image within a page, if
    any," not just per page of text.

    Returns:
        1-indexed page number -> list of temp file paths (caller must
        delete them once moderation has run). Best-effort per image: a
        single unsupported/corrupt embedded image is skipped, logged, and
        never aborts extraction of the rest of the document.
    """
    reader = PdfReader(BytesIO(file_bytes))
    images_by_page: dict[int, list[Path]] = {}
    for page_number, page in enumerate(reader.pages, start=1):
        paths: list[Path] = []
        for image_file in page.images:
            try:
                suffix = Path(image_file.name).suffix or ".bin"
                fd, tmp_path_str = tempfile.mkstemp(suffix=suffix, prefix="pdf_img_")
                os.close(fd)
                tmp_path = Path(tmp_path_str)
                tmp_path.write_bytes(image_file.data)
                paths.append(tmp_path)
            except Exception:  # noqa: BLE001 - one bad embedded image must not abort the page
                logger.warning(
                    "[rag.ingestion] failed to extract an embedded image on page %d",
                    page_number,
                    exc_info=True,
                )
        if paths:
            images_by_page[page_number] = paths
    return images_by_page


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


def _build_moderation_chunks(
    pages: list[str], ocr_text_by_page: dict[int, str], images_by_page: dict[int, list[Path]]
) -> tuple[list[ModerationChunk], list[Path]]:
    """Builds one `ModerationChunk` per page's text (extracted text, or its
    OCR'd text if that page was flagged scanned) plus one per embedded
    image -- per this feature's chunking requirement ("split per page and
    per embedded image within a page").

    Returns:
        `(chunks, temp_image_files)` -- the caller deletes every path in
        the second list once moderation has run.
    """
    chunks: list[ModerationChunk] = []
    temp_files: list[Path] = []
    chunk_index = 0

    for page_number, page_text in enumerate(pages, start=1):
        text_for_page = page_text if page_text.strip() else ocr_text_by_page.get(page_number, "")
        if text_for_page.strip():
            chunks.append(ModerationChunk(chunk_index=chunk_index, content_type="text", text=text_for_page))
            chunk_index += 1

    for page_number, image_paths in images_by_page.items():
        for image_path in image_paths:
            chunks.append(ModerationChunk(chunk_index=chunk_index, content_type="image", image_path=image_path))
            temp_files.append(image_path)
            chunk_index += 1

    return chunks, temp_files


def ingest_pdf(
    file_bytes: bytes,
    filename: str,
    collection: Collection,
    sensitivity_category: SensitivityCategory = None,
    settings: Settings | None = None,
) -> IngestionResult:
    """Runs the full extract -> chunk -> moderate -> embed -> store pipeline
    for one PDF.

    Nothing touches `rag/store.py` (and therefore `rag.documents` never
    gains a row) until moderation has passed -- see this module's docstring
    for why that ordering matters. A `rag.documents` row is created (status
    "processing" immediately, then "ready"/"failed") only once moderation
    has already passed, so a failure partway through the *embedding* step
    still leaves a visible record in the management view, consistent with
    this app's "fail visibly, never silently" posture -- but a moderation
    *rejection* leaves no `rag.documents` row at all, per this feature's
    "reject the entire asset, don't store anything" requirement.

    Raises:
        ModerationNotConfiguredError: the moderation provider and/or
            metadata store aren't configured -- mandatory whenever
            `ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG` is on, so ingestion
            fails closed rather than silently skipping the check.
    """
    settings = settings or get_settings()
    file_hash = _content_hash(file_bytes)

    moderation_engine = get_moderation_engine(settings)
    ensure_moderation_schema(moderation_engine)
    existing = get_asset_by_hash(moderation_engine, file_hash)
    if existing is not None:
        if existing.moderation_status == "rejected":
            return IngestionResult(
                document_id=None,
                filename=filename,
                status="failed",
                chunk_count=0,
                error_message="This file was previously rejected by content moderation.",
            )
        # A passed duplicate -- see this module's docstring for the
        # narrow "same hash, different collection/sensitivity" limitation.
        existing_document_id = existing.vector_ids[0] if existing.vector_ids else None
        logger.info(
            "[rag.ingestion] filename=%r hash=%s already moderated and ingested as document_id=%s, skipping",
            filename,
            file_hash[:12],
            existing_document_id,
        )
        return IngestionResult(
            document_id=existing_document_id,
            filename=filename,
            status="ready",
            chunk_count=existing.chunk_count,
        )

    pages = extract_pdf_pages(file_bytes)
    warnings = [
        f"Page {i}: little to no extractable text -- may be a scanned image "
        "that needs OCR before it can be indexed."
        for i, page_text in enumerate(pages, start=1)
        if len(page_text.strip()) < _OCR_SUSPECT_CHAR_THRESHOLD
    ]

    ocr_text_by_page = _ocr_suspect_pages(file_bytes, pages)
    images_by_page = _extract_embedded_images(file_bytes)
    moderation_chunks, temp_image_files = _build_moderation_chunks(pages, ocr_text_by_page, images_by_page)

    try:
        decision = moderate_chunks(file_hash, moderation_chunks, settings)
    finally:
        for temp_path in temp_image_files:
            temp_path.unlink(missing_ok=True)

    if not decision.passed:
        record_asset(
            moderation_engine,
            file_hash,
            "pdf",
            "rejected",
            decision_summary(decision),
            source_path=filename,
        )
        return IngestionResult(
            document_id=None,
            filename=filename,
            status="failed",
            chunk_count=0,
            warnings=warnings,
            error_message="This file was rejected by content moderation and was not ingested.",
        )

    rag_engine = get_rag_engine(settings)
    ensure_rag_schema(rag_engine)

    document_id = insert_document(
        rag_engine,
        filename,
        collection,
        sensitivity_category=sensitivity_category,
        pdf_bytes=file_bytes if settings.enable_pdf_download else None,
    )

    try:
        # Use OCR'd text in place of a scanned page's (near-)empty
        # extraction for the actual retrieval chunks too, not just for the
        # moderation check above -- an indexed-but-unsearchable scanned
        # page would otherwise contribute nothing to retrieval.
        pages_for_chunking = [
            page_text if page_text.strip() else ocr_text_by_page.get(page_number, "")
            for page_number, page_text in enumerate(pages, start=1)
        ]
        drafts = chunk_pages(pages_for_chunking, settings.rag_chunk_size, settings.rag_chunk_overlap)
        if not drafts:
            update_document_status(
                rag_engine,
                document_id,
                "failed",
                chunk_count=0,
                error_message="No extractable text found in this PDF (it may be scanned/image-only).",
            )
            record_asset(
                moderation_engine,
                file_hash,
                "pdf",
                "passed",
                decision_summary(decision),
                source_path=filename,
                chunk_count=0,
                vector_ids=[document_id],
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
            rag_engine,
            document_id,
            [
                (draft.text, draft.index, embedding, draft.page_number)
                for draft, embedding in zip(drafts, embeddings, strict=True)
            ],
        )
        update_document_status(rag_engine, document_id, "ready", chunk_count=len(drafts))
        record_asset(
            moderation_engine,
            file_hash,
            "pdf",
            "passed",
            decision_summary(decision),
            source_path=filename,
            chunk_count=len(drafts),
            vector_ids=[document_id],
        )
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
        update_document_status(rag_engine, document_id, "failed", chunk_count=0, error_message=str(exc))
        return IngestionResult(
            document_id=document_id,
            filename=filename,
            status="failed",
            chunk_count=0,
            error_message=str(exc),
        )
