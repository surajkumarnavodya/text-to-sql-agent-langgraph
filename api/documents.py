"""FastAPI router for Knowledge Sources document management.

Backs `frontend/src/pages/KnowledgeSources.tsx`'s upload/list/delete/
download UI. Mounted onto `api/main.py`'s `app` as a thin, additive
extension -- every route here calls the exact same `rag.ingestion`/
`rag.store` functions directly, so there is no second implementation of
ingestion, storage, or the sensitivity gate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response

from api.auth import verify_api_key
from api.rate_limit import enforce_api_action_rate_limit
from api.schemas import DocumentListResponse, DocumentOut, DocumentUploadResponse
from config.settings import get_settings
from rag.ingestion import ingest_pdf
from rag.store import (
    Collection,
    RagStoreNotConfiguredError,
    SensitivityCategory,
    delete_document,
    ensure_schema,
    get_document_bytes,
    get_rag_engine,
    list_documents,
)

router = APIRouter(dependencies=[Depends(verify_api_key)])


def _require_collection_configured(collection: Collection) -> None:
    settings = get_settings()
    enabled = (
        settings.enable_document_rag if collection == "documents" else settings.enable_policy_rag
    )
    if not enabled:
        flag = "ENABLE_DOCUMENT_RAG" if collection == "documents" else "ENABLE_POLICY_RAG"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"The {collection!r} collection is disabled. Set {flag}=true in .env.",
        )


@router.get("/documents", response_model=DocumentListResponse)
def list_documents_route(collection: Collection | None = None) -> DocumentListResponse:
    """Lists ingested documents, optionally filtered to one collection --
    mirrors the Knowledge Sources page's per-tab document table."""
    if collection is not None:
        _require_collection_configured(collection)
    settings = get_settings()
    try:
        engine = get_rag_engine(settings)
        ensure_schema(engine)
    except RagStoreNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return DocumentListResponse(
        documents=[
            DocumentOut(
                id=doc.id,
                filename=doc.filename,
                collection=doc.collection,
                sensitivity_category=doc.sensitivity_category,
                upload_date=doc.upload_date,
                status=doc.status,
                chunk_count=doc.chunk_count,
                error_message=doc.error_message,
                has_pdf_bytes=doc.has_pdf_bytes,
            )
            for doc in list_documents(engine, collection=collection)
        ]
    )


@router.post("/documents", response_model=DocumentUploadResponse)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    collection: Collection = Form(...),
    sensitivity_category: SensitivityCategory = Form(default=None),
) -> DocumentUploadResponse:
    """Ingests one PDF into the "documents" or "policies" collection --
    mirrors the Knowledge Sources page's upload form, including the
    optional sensitivity-category selector used for "policies" uploads.

    Rate-limited per client IP (`enforce_api_action_rate_limit`) -- PDF
    ingestion (extract, chunk, embed) is real, non-trivial work. Reads at
    most `max_document_upload_mb + 1` bytes regardless of how large the
    actual upload claims to be (rather than `file.read()`'s previous
    unbounded read), and rejects anything not actually PDF-shaped by magic
    bytes -- previously the only "validation" was `pypdf` failing to parse
    non-PDF content after the fact.
    """
    settings = get_settings()
    enforce_api_action_rate_limit(request, "document_upload", settings)
    _require_collection_configured(collection)

    max_bytes = settings.max_document_upload_mb * 1024 * 1024
    file_bytes = await file.read(max_bytes + 1)
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File exceeds the {settings.max_document_upload_mb}MB upload limit.",
        )
    if not file_bytes.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF files are supported (the uploaded file isn't PDF-shaped).",
        )

    try:
        result = ingest_pdf(
            file_bytes,
            file.filename or "upload.pdf",
            collection,
            sensitivity_category=sensitivity_category,
            settings=settings,
        )
    except RagStoreNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return DocumentUploadResponse(
        document_id=result.document_id,
        filename=result.filename,
        status=result.status,  # type: ignore[arg-type]
        chunk_count=result.chunk_count,
        warnings=result.warnings,
        error_message=result.error_message,
    )


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document_route(document_id: str, request: Request) -> None:
    """Deletes a document and its chunks -- mirrors the management page's
    unconditional delete button (this page's admin surface is already
    fully-privileged/no-per-user-authorization; see CLAUDE.md's
    "Document/policy agentic RAG" section). Rate-limited per client IP
    (`enforce_api_action_rate_limit`) -- an irreversible, previously-
    unrated action."""
    settings = get_settings()
    enforce_api_action_rate_limit(request, "document_delete", settings)
    try:
        engine = get_rag_engine(settings)
    except RagStoreNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    delete_document(engine, document_id)


@router.get("/documents/{document_id}/download")
def download_document(document_id: str) -> Response:
    """Streams a document's original PDF bytes -- mirrors the management
    page's per-document download button and the chat citation download
    button. Callers reaching this from a chat citation inherit the
    sensitivity gate for free: a restricted policy match's citations list
    is always empty (see `rag/graph.py`), so a client never has a
    `document_id` to call this with for a restricted document in the first
    place. This route itself has no separate access check, matching the
    Knowledge Sources page's own already-fully-privileged exposure."""
    try:
        engine = get_rag_engine(get_settings())
    except RagStoreNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    pdf_bytes = get_document_bytes(engine, document_id)
    if pdf_bytes is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No PDF stored for this document."
        )
    return Response(content=pdf_bytes, media_type="application/pdf")
