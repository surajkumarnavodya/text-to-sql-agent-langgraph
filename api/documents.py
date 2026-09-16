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
from moderation.exceptions import ModerationNotConfiguredError

from agent.authz import Permission, has_permission
from api.authz import require_permission
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
    get_document_restricted_roles,
    get_document_sensitivity,
    get_rag_engine,
    list_documents,
)
from security.audit_log import log_security_event

router = APIRouter()


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
def list_documents_route(
    collection: Collection | None = None,
    _identity=Depends(require_permission(Permission.DOCUMENTS_READ)),
) -> DocumentListResponse:
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
                uploaded_by=doc.uploaded_by,
                restricted_roles=list(doc.restricted_roles) if doc.restricted_roles else None,
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
    # 2026 Phase 3 security review: an optional, admin-set comma-separated
    # role list restricting this document beyond the fixed sensitivity
    # categories above -- see rag/store.py's DocumentRecord docstring and
    # rag/graph.py's role-restriction gate. Empty/unset means "unrestricted"
    # (the pre-existing, shared-knowledge-base default), matching this
    # form's existing sensitivity_category convention of "None means no
    # restriction."
    restricted_roles: str | None = Form(default=None),
    identity=Depends(require_permission(Permission.DOCUMENTS_WRITE)),
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

    parsed_restricted_roles = (
        tuple(role.strip() for role in restricted_roles.split(",") if role.strip())
        if restricted_roles
        else None
    ) or None

    try:
        result = ingest_pdf(
            file_bytes,
            file.filename or "upload.pdf",
            collection,
            sensitivity_category=sensitivity_category,
            settings=settings,
            uploaded_by=identity.subject,
            restricted_roles=parsed_restricted_roles,
        )
    except (RagStoreNotConfiguredError, ModerationNotConfiguredError) as exc:
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
def delete_document_route(
    document_id: str,
    request: Request,
    _identity=Depends(require_permission(Permission.DOCUMENTS_DELETE)),
) -> None:
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
def download_document(
    document_id: str,
    identity=Depends(require_permission(Permission.DOCUMENTS_READ)),
) -> Response:
    """Streams a document's original PDF bytes -- mirrors the management
    page's per-document download button and the chat citation download
    button. Callers reaching this from a chat citation inherit the
    `rag/graph.py` sensitivity gate for free (a restricted policy match's
    citations list is always empty, so a client never has a `document_id`
    to call this with for a restricted document via that path) -- but that
    was always a narrower control specific to the *chat answer*, not this
    route.

    2026 Phase 2 security review: this route previously had no separate
    access check of its own beyond the general document-management
    permission, so a sensitivity-tagged ("compensation"/"disciplinary"/
    "legal") document's raw bytes were downloadable by anyone who could
    reach the API at all. A caller now additionally needs
    `Permission.DOCUMENTS_READ_SENSITIVE` for a document whose
    `sensitivity_category` is set, checked via `get_document_sensitivity`
    before the response is ever built -- the bytes never leave this
    process to a caller who fails that check, even though (to keep the
    existing "no PDF stored" 404 behavior for a genuinely missing document
    exactly as it was) the lookup runs after `get_document_bytes` rather
    than instead of it.

    2026 Phase 3 security review: the same shape of check, for the more
    general `restricted_roles` field (`rag/store.py::DocumentRecord`'s
    docstring) -- a document restricted to specific roles is just as
    downloadable-by-anyone as a sensitivity-tagged one was before the Phase
    2 fix above, unless this route independently enforces it too (the
    `rag/graph.py` chat-answer gate is, as noted above, a narrower control
    that doesn't cover this route).
    """
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

    sensitivity = get_document_sensitivity(engine, document_id)
    if sensitivity and not has_permission(identity, Permission.DOCUMENTS_READ_SENSITIVE):
        log_security_event(
            "authz_denied",
            "warning",
            "A request to download a sensitivity-tagged document was denied.",
            subject=identity.subject,
            roles=list(identity.roles),
            required_permission=Permission.DOCUMENTS_READ_SENSITIVE.value,
            document_id=document_id,
            sensitivity_category=sensitivity,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to download this document.",
        )

    restricted_roles = get_document_restricted_roles(engine, document_id)
    if restricted_roles and not (set(restricted_roles) & set(identity.roles)):
        log_security_event(
            "authz_denied",
            "warning",
            "A request to download a role-restricted document was denied.",
            subject=identity.subject,
            roles=list(identity.roles),
            document_id=document_id,
            restricted_roles=list(restricted_roles),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to download this document.",
        )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"X-Content-Type-Options": "nosniff"},
    )
