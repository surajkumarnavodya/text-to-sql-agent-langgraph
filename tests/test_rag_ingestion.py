"""Unit tests for rag/ingestion.py -- scoped to the moderation-gate
integration this change added, not a full RAG ingestion test suite
(`rag/` has no pre-existing pytest coverage for `ingest_pdf` at all -- see
CLAUDE.md's "Known gaps"; this closes the part of that gap this change
touches). Every `rag/store.py`/`moderation/*` call site is mocked -- no
real SQL Server, embedding model, or moderation provider is ever touched.
"""

from __future__ import annotations

import pytest

from config.settings import Settings
from moderation.store import MediaAssetRecord
from moderation.types import ModerationDecision
from rag.ingestion import _content_hash, chunk_pages, extract_pdf_pages, ingest_pdf


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"rag_chunk_size": 1200, "rag_chunk_overlap": 150}
    base.update(overrides)
    return Settings(**base)


def _mock_moderation_plumbing(monkeypatch, *, existing=None, decision=None):
    """Mirrors `tests/test_media_ingest.py`'s helper of the same name/shape,
    for `rag/ingestion.py`'s identical moderation-store call sites."""
    monkeypatch.setattr("rag.ingestion.get_moderation_engine", lambda settings: object())
    monkeypatch.setattr("rag.ingestion.ensure_moderation_schema", lambda engine: None)
    monkeypatch.setattr("rag.ingestion.get_asset_by_hash", lambda engine, file_hash: existing)
    recorded: list[dict] = []
    monkeypatch.setattr(
        "rag.ingestion.record_asset",
        lambda engine, file_hash, media_type, status, checks, **kw: (
            recorded.append(
                {"file_hash": file_hash, "media_type": media_type, "status": status, **kw}
            ),
            "fake-asset-id",
        )[1],
    )
    monkeypatch.setattr(
        "rag.ingestion.moderate_chunks",
        lambda asset_hash, chunks, settings: decision or ModerationDecision(status="passed"),
    )
    return recorded


def _mock_rag_plumbing(monkeypatch, *, document_id: str = "doc-1"):
    monkeypatch.setattr("rag.ingestion.get_rag_engine", lambda settings: object())
    monkeypatch.setattr("rag.ingestion.ensure_rag_schema", lambda engine: None)
    monkeypatch.setattr(
        "rag.ingestion.insert_document",
        lambda engine, filename, collection, sensitivity_category=None, pdf_bytes=None: document_id,
    )
    status_updates: list[dict] = []
    monkeypatch.setattr(
        "rag.ingestion.update_document_status",
        lambda engine, doc_id, status, chunk_count=None, error_message=None: status_updates.append(
            {"document_id": doc_id, "status": status, "chunk_count": chunk_count, "error_message": error_message}
        ),
    )
    inserted_chunks: list = []
    monkeypatch.setattr(
        "rag.ingestion.insert_chunks",
        lambda engine, doc_id, chunks: inserted_chunks.extend(chunks),
    )
    monkeypatch.setattr("rag.ingestion.embed_texts", lambda texts, settings: [[0.1] for _ in texts])
    return status_updates, inserted_chunks


# A minimal, real one-page PDF (extractable text "Hello world") so
# extract_pdf_pages/chunk_pages exercise real pypdf parsing rather than a
# fake stand-in -- generated once via pypdf's own writer at import time
# would be overkill; a fixture-style constant is simpler and just as real.
def _sample_pdf_bytes() -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestContentHash:
    def test_deterministic_for_identical_bytes(self):
        assert _content_hash(b"same content") == _content_hash(b"same content")

    def test_differs_for_different_bytes(self):
        assert _content_hash(b"one") != _content_hash(b"two")


class TestIngestPdfDedupe:
    def test_a_previously_passed_duplicate_short_circuits_before_extraction(self, monkeypatch):
        file_bytes = _sample_pdf_bytes()
        existing = MediaAssetRecord(
            asset_id="existing",
            file_hash=_content_hash(file_bytes),
            media_type="pdf",
            moderation_status="passed",
            moderation_checks={},
            chunk_count=4,
            vector_ids=["existing-doc-id"],
        )
        _mock_moderation_plumbing(monkeypatch, existing=existing)

        def _fail(*args, **kwargs):
            raise AssertionError("extract_pdf_pages must not run for an already-moderated duplicate")

        monkeypatch.setattr("rag.ingestion.extract_pdf_pages", _fail)

        result = ingest_pdf(file_bytes, "f.pdf", "documents", settings=_settings())

        assert result.status == "ready"
        assert result.document_id == "existing-doc-id"
        assert result.chunk_count == 4

    def test_a_previously_rejected_duplicate_short_circuits_as_failed(self, monkeypatch):
        file_bytes = _sample_pdf_bytes()
        existing = MediaAssetRecord(
            asset_id="existing",
            file_hash=_content_hash(file_bytes),
            media_type="pdf",
            moderation_status="rejected",
            moderation_checks={"triggering_categories": ["violence"]},
            chunk_count=0,
            vector_ids=None,
        )
        _mock_moderation_plumbing(monkeypatch, existing=existing)

        def _fail(*args, **kwargs):
            raise AssertionError("extract_pdf_pages must not run for a known-rejected duplicate")

        monkeypatch.setattr("rag.ingestion.extract_pdf_pages", _fail)

        result = ingest_pdf(file_bytes, "f.pdf", "documents", settings=_settings())

        assert result.status == "failed"
        assert result.document_id is None


class TestIngestPdfModerationReject:
    def test_a_rejection_never_calls_insert_document(self, monkeypatch):
        file_bytes = _sample_pdf_bytes()
        recorded = _mock_moderation_plumbing(
            monkeypatch,
            decision=ModerationDecision(status="rejected", triggering_categories=("hate",)),
        )

        def _fail(*args, **kwargs):
            raise AssertionError("insert_document must not be called for a rejected PDF")

        monkeypatch.setattr("rag.ingestion.insert_document", _fail)
        monkeypatch.setattr("rag.ingestion.get_rag_engine", lambda settings: object())
        monkeypatch.setattr("rag.ingestion.ensure_rag_schema", lambda engine: None)

        result = ingest_pdf(file_bytes, "f.pdf", "documents", settings=_settings())

        assert result.status == "failed"
        assert result.document_id is None
        assert recorded[0]["status"] == "rejected"
        assert "vector_ids" not in recorded[0]


class TestIngestPdfModerationPass:
    def test_a_pass_ingests_normally_and_records_the_asset(self, monkeypatch):
        file_bytes = _sample_pdf_bytes()
        # A blank page has no extractable text -- chunk_pages would return
        # [] and the "no extractable text" branch would fire, which is a
        # valid, separately-covered path but not what this test wants to
        # exercise. Feed real page text via chunk_pages directly instead of
        # relying on the blank sample PDF's own (empty) extraction.
        monkeypatch.setattr("rag.ingestion.extract_pdf_pages", lambda file_bytes: ["Hello world, page one."])
        monkeypatch.setattr("rag.ingestion._ocr_suspect_pages", lambda file_bytes, pages: {})
        monkeypatch.setattr("rag.ingestion._extract_embedded_images", lambda file_bytes: {})

        recorded = _mock_moderation_plumbing(monkeypatch)
        status_updates, inserted_chunks = _mock_rag_plumbing(monkeypatch, document_id="doc-42")

        result = ingest_pdf(file_bytes, "f.pdf", "documents", settings=_settings())

        assert result.status == "ready"
        assert result.document_id == "doc-42"
        assert result.chunk_count == 1
        assert inserted_chunks[0][0] == "Hello world, page one."
        assert status_updates[-1]["status"] == "ready"
        assert recorded[0]["status"] == "passed"
        assert recorded[0]["vector_ids"] == ["doc-42"]


class TestExtractAndChunk:
    def test_extract_pdf_pages_returns_one_entry_per_page(self):
        pages = extract_pdf_pages(_sample_pdf_bytes())
        assert len(pages) == 1

    def test_chunk_pages_tracks_page_numbers(self):
        chunks = chunk_pages(["first page text here"], chunk_size=1000, overlap=0)
        assert len(chunks) == 1
        assert chunks[0].page_number == 1
