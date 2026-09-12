"""Unit tests for the /documents routes (api/documents.py) -- the API
equivalent of `ui/pages/1_Knowledge_Sources.py`'s upload/list/delete/
download UI.

Fully mocked: `rag.ingestion.ingest_pdf` and every `rag.store` function are
patched at the `api.documents` module they're looked up from, mirroring
`tests/test_api_ask.py`'s patching convention. No real RAG store connection
is ever touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from config.settings import Settings
from rag.ingestion import IngestionResult
from rag.store import DocumentRecord, RagStoreNotConfiguredError
from security.secrets import SecretStr

_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="postgresql",
    db_host="db.example.com",
    db_port=5432,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("secret"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="x",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="schema_ddl",
    embedding_model_name="all-MiniLM-L6-v2",
    schema_top_k=4,
    max_retries=3,
    complex_query_max_retry_bonus=2,
    max_result_rows=1000,
    query_timeout_seconds=15,
    llm_max_tokens=1024,
    insight_max_tokens=120,
    max_question_length=500,
    question_rate_limit_per_minute=10,
    llm_call_rate_limit_per_minute=20,
    cost_estimation_enabled=True,
    cost_estimation_timeout_seconds=3,
    cost_moderate_row_threshold=50_000,
    cost_high_row_threshold=1_000_000,
    log_level="INFO",
    log_redaction_level="standard",
    enable_document_rag=True,
    enable_policy_rag=True,
    rag_store_connection_string=SecretStr("mssql+pyodbc://x"),
)


@pytest.fixture(autouse=True)
def _mock_settings(monkeypatch):
    monkeypatch.setattr("api.documents.get_settings", lambda: _BASE_SETTINGS)
    monkeypatch.setattr("api.documents.get_rag_engine", lambda settings: object())
    monkeypatch.setattr("api.documents.ensure_schema", lambda engine: None)
    return _BASE_SETTINGS


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestListDocuments:
    def test_returns_documents_for_the_requested_collection(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.documents.list_documents",
            lambda engine, collection=None: [
                DocumentRecord(
                    id="doc-1",
                    filename="leave.pdf",
                    collection="policies",
                    sensitivity_category=None,
                    upload_date="2026-01-01",
                    status="ready",
                    chunk_count=3,
                    error_message=None,
                    has_pdf_bytes=True,
                )
            ],
        )

        response = client.get("/documents", params={"collection": "policies"})

        assert response.status_code == 200
        body = response.json()
        assert len(body["documents"]) == 1
        assert body["documents"][0]["filename"] == "leave.pdf"
        assert body["documents"][0]["has_pdf_bytes"] is True

    def test_disabled_collection_returns_404(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.documents.get_settings",
            lambda: Settings(**{**_BASE_SETTINGS.__dict__, "enable_policy_rag": False}),
        )

        response = client.get("/documents", params={"collection": "policies"})

        assert response.status_code == 404

    def test_not_configured_store_returns_503(self, monkeypatch, client):
        def _raise(settings):
            raise RagStoreNotConfiguredError("RAG_STORE_CONNECTION_STRING is not set.")

        monkeypatch.setattr("api.documents.get_rag_engine", _raise)

        response = client.get("/documents")

        assert response.status_code == 503


class TestUploadDocument:
    def test_uploads_and_ingests_a_pdf(self, monkeypatch, client):
        captured = {}

        def _ingest(file_bytes, filename, collection, sensitivity_category=None, settings=None):
            captured["filename"] = filename
            captured["collection"] = collection
            captured["sensitivity_category"] = sensitivity_category
            return IngestionResult(
                document_id="doc-2", filename=filename, status="ready", chunk_count=5
            )

        monkeypatch.setattr("api.documents.ingest_pdf", _ingest)

        response = client.post(
            "/documents",
            files={"file": ("policy.pdf", b"%PDF-1.4 ...", "application/pdf")},
            data={"collection": "policies", "sensitivity_category": "compensation"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["chunk_count"] == 5
        assert captured["collection"] == "policies"
        assert captured["sensitivity_category"] == "compensation"

    def test_invalid_sensitivity_category_is_rejected_by_request_validation(
        self, monkeypatch, client
    ):
        """`sensitivity_category`'s Literal type means FastAPI/Pydantic
        reject an unrecognized value before the handler (and therefore
        `ingest_pdf`) ever runs -- a stricter net than `insert_document`'s
        own `ValueError` guard, not a bypass of it."""
        called = False

        def _fail(*a, **k):
            nonlocal called
            called = True

        monkeypatch.setattr("api.documents.ingest_pdf", _fail)

        response = client.post(
            "/documents",
            files={"file": ("f.pdf", b"%PDF-1.4", "application/pdf")},
            data={"collection": "policies", "sensitivity_category": "bogus"},
        )

        assert response.status_code == 422
        assert not called


class TestDeleteDocument:
    def test_deletes_and_returns_204(self, monkeypatch, client):
        called: dict[str, str] = {}
        monkeypatch.setattr(
            "api.documents.delete_document",
            lambda engine, document_id: called.setdefault("document_id", document_id),
        )

        response = client.delete("/documents/doc-1")

        assert response.status_code == 204
        assert called["document_id"] == "doc-1"


class TestDownloadDocument:
    def test_returns_pdf_bytes(self, monkeypatch, client):
        monkeypatch.setattr(
            "api.documents.get_document_bytes", lambda engine, document_id: b"%PDF-1.4 raw bytes"
        )

        response = client.get("/documents/doc-1/download")

        assert response.status_code == 200
        assert response.content == b"%PDF-1.4 raw bytes"
        assert response.headers["content-type"] == "application/pdf"

    def test_missing_pdf_returns_404(self, monkeypatch, client):
        monkeypatch.setattr("api.documents.get_document_bytes", lambda engine, document_id: None)

        response = client.get("/documents/doc-1/download")

        assert response.status_code == 404
