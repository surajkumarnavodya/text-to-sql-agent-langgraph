"""Unit tests for the PDF-download feature added to the RAG store/graph.

`rag/` has no existing mocked pytest suite (see CLAUDE.md's "Known gaps" --
it was verified against a real SQL Server instance during development
instead); these are scoped narrowly to the new pure logic this feature
added, mirroring the MagicMock-engine style already used elsewhere in this
suite (e.g. `tests/test_write_privilege_check.py`) rather than attempting
to backfill the rest of `rag/`'s untested surface.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from rag.graph import Citation, _generate_node
from rag.store import ChunkResult, get_document_bytes, insert_document, list_documents


def _mock_engine(connection: MagicMock) -> MagicMock:
    """A MagicMock Engine whose `.begin()`/`.connect()` both yield `connection`
    as a context manager -- mirrors `tests/test_write_privilege_check.py`'s
    `_mock_engine` pattern, extended to cover both entry points `rag/store.py`
    actually uses (`.begin()` for writes, `.connect()` for reads)."""
    engine = MagicMock()
    for entry_point in (engine.begin, engine.connect):
        entry_point.return_value.__enter__.return_value = connection
        entry_point.return_value.__exit__.return_value = False
    return engine


class TestInsertDocumentPdfBytes:
    def test_binds_pdf_bytes_when_provided(self):
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = "doc-id"
        engine = _mock_engine(connection)

        insert_document(engine, "f.pdf", "documents", pdf_bytes=b"%PDF-1.4 ...")

        args, _ = connection.execute.call_args
        assert args[1]["pdf_bytes"] == b"%PDF-1.4 ..."

    def test_binds_none_when_not_provided(self):
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = "doc-id"
        engine = _mock_engine(connection)

        insert_document(engine, "f.pdf", "documents")

        args, _ = connection.execute.call_args
        assert args[1]["pdf_bytes"] is None


class TestGetDocumentBytes:
    def test_returns_bytes_when_present(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = MagicMock(pdf_bytes=b"raw-bytes")
        engine = _mock_engine(connection)

        assert get_document_bytes(engine, "doc-id") == b"raw-bytes"

    def test_returns_none_when_row_missing(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        engine = _mock_engine(connection)

        assert get_document_bytes(engine, "doc-id") is None

    def test_returns_none_when_column_is_null(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = MagicMock(pdf_bytes=None)
        engine = _mock_engine(connection)

        assert get_document_bytes(engine, "doc-id") is None


class TestListDocumentsHasPdfBytes:
    def test_projects_has_pdf_bytes_true(self):
        connection = MagicMock()
        row = MagicMock(
            id="doc-id",
            filename="f.pdf",
            collection="documents",
            sensitivity_category=None,
            upload_date="2026-01-01",
            status="ready",
            chunk_count=3,
            error_message=None,
            has_pdf_bytes=1,
        )
        connection.execute.return_value.fetchall.return_value = [row]
        engine = _mock_engine(connection)

        [record] = list_documents(engine)

        assert record.has_pdf_bytes is True

    def test_projects_has_pdf_bytes_false(self):
        connection = MagicMock()
        row = MagicMock(
            id="doc-id",
            filename="f.pdf",
            collection="documents",
            sensitivity_category=None,
            upload_date="2026-01-01",
            status="ready",
            chunk_count=3,
            error_message=None,
            has_pdf_bytes=0,
        )
        connection.execute.return_value.fetchall.return_value = [row]
        engine = _mock_engine(connection)

        [record] = list_documents(engine)

        assert record.has_pdf_bytes is False


class TestEnsureSchemaMigratesExistingTables:
    def test_issues_an_idempotent_column_migration_guard(self):
        from rag.store import ensure_schema

        connection = MagicMock()
        engine = _mock_engine(connection)

        ensure_schema(engine)

        executed_sql = [str(call.args[0]) for call in connection.execute.call_args_list]
        migration_statements = [
            sql for sql in executed_sql if "pdf_bytes" in sql and "ALTER TABLE" in sql
        ]
        assert len(migration_statements) == 1
        # Idempotent -- guarded the same way the CREATE TABLE blocks are,
        # so it's a no-op on both a fresh install and a repeat call.
        assert "IF NOT EXISTS" in migration_statements[0]


def _chunk(document_id: str, has_pdf_bytes: bool, sensitivity_category=None) -> ChunkResult:
    return ChunkResult(
        chunk_text="some excerpt",
        similarity=0.9,
        document_id=document_id,
        filename="f.pdf",
        chunk_index=0,
        page_number=1,
        sensitivity_category=sensitivity_category,
        has_pdf_bytes=has_pdf_bytes,
    )


class TestGenerateNodeCitationFields:
    def test_populates_document_id_and_has_pdf_bytes(self, monkeypatch):
        monkeypatch.setattr("rag.llm.call_ollama", lambda *a, **k: "an answer")
        settings = MagicMock()

        result = _generate_node(
            {"question": "q?", "chunks": [_chunk("doc-1", has_pdf_bytes=True)]}, settings=settings
        )

        citations: list[Citation] = result["citations"]
        assert citations[0]["document_id"] == "doc-1"
        assert citations[0]["has_pdf_bytes"] is True

    def test_restricted_result_still_has_no_citations_at_all(self, monkeypatch):
        """Regression guard: the new document_id/has_pdf_bytes fields must
        not leak a restricted document's identity through any side path --
        citations must still be reset to [] entirely."""

        def _fail(*args, **kwargs):
            raise AssertionError("must not call the LLM for a restricted result")

        monkeypatch.setattr("rag.llm.call_ollama", _fail)
        settings = MagicMock()

        result = _generate_node(
            {
                "question": "what's the compensation policy?",
                "chunks": [
                    _chunk("doc-1", has_pdf_bytes=True, sensitivity_category="compensation")
                ],
            },
            settings=settings,
        )

        assert result["citations"] == []
        assert result["status"] == "restricted"
