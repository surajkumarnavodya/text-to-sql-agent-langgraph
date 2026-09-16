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
from rag.store import (
    ChunkResult,
    get_document_bytes,
    insert_document,
    list_documents,
    similarity_search,
)


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


class TestRolesCsvRoundTrip:
    """2026 Phase 3 security review: the comma-joined storage encoding for
    `restricted_roles`, used by both `insert_document` (write) and
    `list_documents`/`similarity_search` (read)."""

    def test_roles_to_csv_joins(self):
        from rag.store import _roles_to_csv

        assert _roles_to_csv(("analyst", "admin")) == "analyst,admin"

    def test_roles_to_csv_none_for_empty_or_none(self):
        from rag.store import _roles_to_csv

        assert _roles_to_csv(None) is None
        assert _roles_to_csv(()) is None

    def test_parse_roles_csv_round_trips(self):
        from rag.store import _parse_roles_csv, _roles_to_csv

        original = ("analyst", "admin")
        assert _parse_roles_csv(_roles_to_csv(original)) == original

    def test_parse_roles_csv_strips_whitespace_and_drops_empties(self):
        from rag.store import _parse_roles_csv

        assert _parse_roles_csv("analyst, admin ,, user") == ("analyst", "admin", "user")

    def test_parse_roles_csv_none_for_none_or_empty_string(self):
        from rag.store import _parse_roles_csv

        assert _parse_roles_csv(None) is None
        assert _parse_roles_csv("") is None


class TestInsertDocumentUploaderAndRestrictedRoles:
    def test_binds_uploaded_by_when_provided(self):
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = "doc-id"
        engine = _mock_engine(connection)

        insert_document(engine, "f.pdf", "documents", uploaded_by="user-42")

        args, _ = connection.execute.call_args
        assert args[1]["uploaded_by"] == "user-42"

    def test_binds_none_uploaded_by_when_not_provided(self):
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = "doc-id"
        engine = _mock_engine(connection)

        insert_document(engine, "f.pdf", "documents")

        args, _ = connection.execute.call_args
        assert args[1]["uploaded_by"] is None

    def test_restricted_roles_bound_as_csv(self):
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = "doc-id"
        engine = _mock_engine(connection)

        insert_document(engine, "f.pdf", "documents", restricted_roles=("analyst", "admin"))

        args, _ = connection.execute.call_args
        assert args[1]["restricted_roles"] == "analyst,admin"

    def test_restricted_roles_none_when_not_provided(self):
        connection = MagicMock()
        connection.execute.return_value.scalar_one.return_value = "doc-id"
        engine = _mock_engine(connection)

        insert_document(engine, "f.pdf", "documents")

        args, _ = connection.execute.call_args
        assert args[1]["restricted_roles"] is None


class TestListDocumentsUploaderAndRestrictedRoles:
    def test_parses_restricted_roles_and_uploaded_by(self):
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
            uploaded_by="user-1",
            restricted_roles="analyst,admin",
        )
        connection.execute.return_value.fetchall.return_value = [row]
        engine = _mock_engine(connection)

        [record] = list_documents(engine)

        assert record.uploaded_by == "user-1"
        assert record.restricted_roles == ("analyst", "admin")

    def test_null_restricted_roles_is_none_not_empty_tuple(self):
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
            uploaded_by=None,
            restricted_roles=None,
        )
        connection.execute.return_value.fetchall.return_value = [row]
        engine = _mock_engine(connection)

        [record] = list_documents(engine)

        assert record.uploaded_by is None
        assert record.restricted_roles is None


class TestGetDocumentRestrictedRoles:
    def test_returns_parsed_roles(self):
        from rag.store import get_document_restricted_roles

        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = MagicMock(
            restricted_roles="analyst,admin"
        )
        engine = _mock_engine(connection)

        assert get_document_restricted_roles(engine, "doc-id") == ("analyst", "admin")

    def test_returns_none_when_unrestricted(self):
        from rag.store import get_document_restricted_roles

        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = MagicMock(restricted_roles=None)
        engine = _mock_engine(connection)

        assert get_document_restricted_roles(engine, "doc-id") is None

    def test_returns_none_when_document_missing(self):
        from rag.store import get_document_restricted_roles

        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = None
        engine = _mock_engine(connection)

        assert get_document_restricted_roles(engine, "doc-id") is None


class TestSimilaritySearchRestrictedRoles:
    """`_generate_node`'s role-restriction gate only sees whatever
    `similarity_search` returns -- confirms the SQL projection round-trips
    `restricted_roles` correctly, matching `list_documents`'s own handling."""

    def test_parses_restricted_roles_from_query_row(self):
        connection = MagicMock()
        row = MagicMock(
            chunk_text="excerpt",
            chunk_index=0,
            page_number=1,
            document_id="doc-1",
            filename="f.pdf",
            sensitivity_category=None,
            has_pdf_bytes=0,
            restricted_roles="analyst,admin",
            distance=0.1,
        )
        connection.execute.return_value.fetchall.return_value = [row]
        engine = _mock_engine(connection)

        [chunk] = similarity_search(engine, "documents", [0.1, 0.2], top_k=5)

        assert chunk.restricted_roles == ("analyst", "admin")

    def test_null_restricted_roles_is_none(self):
        connection = MagicMock()
        row = MagicMock(
            chunk_text="excerpt",
            chunk_index=0,
            page_number=1,
            document_id="doc-1",
            filename="f.pdf",
            sensitivity_category=None,
            has_pdf_bytes=0,
            restricted_roles=None,
            distance=0.1,
        )
        connection.execute.return_value.fetchall.return_value = [row]
        engine = _mock_engine(connection)

        [chunk] = similarity_search(engine, "documents", [0.1, 0.2], top_k=5)

        assert chunk.restricted_roles is None


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

    def test_2026_phase3_columns_get_the_same_idempotent_migration_guard(self):
        """uploaded_by/restricted_roles (added alongside the
        restricted_roles feature) -- same pattern as pdf_bytes above, for
        an existing pre-upgrade deployment's table."""
        from rag.store import ensure_schema

        connection = MagicMock()
        engine = _mock_engine(connection)

        ensure_schema(engine)

        executed_sql = [str(call.args[0]) for call in connection.execute.call_args_list]
        for column in ("uploaded_by", "restricted_roles"):
            migration_statements = [
                sql for sql in executed_sql if column in sql and "ALTER TABLE" in sql
            ]
            assert len(migration_statements) == 1, f"expected exactly one migration for {column}"
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
