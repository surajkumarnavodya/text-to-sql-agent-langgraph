"""FastAPI router for chat attachment upload/management.

Backs the composer's "attach a file" flow -- every route here calls
`attachments.pipeline` functions directly, so there is no second
implementation of validation/storage/processing. Mounted onto
`api/main.py`'s `app` as a thin, additive extension, the same shape
`api/documents.py`'s router already is.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from agent.authz import Permission
from api.authz import require_permission
from api.rate_limit import enforce_api_action_rate_limit
from api.schemas import (
    AttachmentCapabilitiesResponse,
    AttachmentErrorOut,
    AttachmentOut,
    AttachmentUploadResponse,
    DetectTextRegionsResponse,
    ImageEditResultResponse,
    ImageResizePresetOut,
    ImageResizeRequest,
    OcrExtractResponse,
    RemoveTextRequest,
    TextLineRegionOut,
    TextRegionOut,
)
from attachments.capabilities import get_attachment_capabilities
from attachments.image_ops import resize_image
from attachments.image_processing import ImageDecodeError
from attachments.inpaint import InpaintRegion, detect_text_line_regions, remove_text
from attachments.ocr_extract import extract_text_with_regions
from attachments.pipeline import (
    delete_attachment,
    register_derived_image,
    validate_and_store_upload,
)
from attachments.store import get_default_attachment_store
from attachments.validation import IMAGE_MEDIA_TYPES, compute_sha256
from config.settings import get_settings
from security.oidc import AuthIdentity, real_caller_subject

router = APIRouter()


def _owner_subject(identity: AuthIdentity) -> str | None:
    """The same real-per-caller-identity scoping `api/main.py`'s `ask()`
    handler uses for `AgentState.caller_subject` (`security.oidc
    .real_caller_subject`) -- an attachment uploaded and later referenced
    via `/ask` must resolve to the same owner identity either way, or the
    ownership check in `attachments.store.AttachmentStore` would reject the
    caller's own upload."""
    return real_caller_subject(identity)


@router.post("/attachments/upload", response_model=AttachmentUploadResponse)
async def upload_attachments(
    request: Request,
    files: list[UploadFile] = File(...),
    identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> AttachmentUploadResponse:
    """Validates, malware-scans, stores, and eagerly processes every
    uploaded file -- called immediately on file selection in the composer,
    before the user submits any question (see `attachments.pipeline
    .validate_and_store_upload`'s own docstring for why processing happens
    now rather than lazily at `/ask` time).

    Rate-limited per client IP, the same `enforce_api_action_rate_limit`
    every other real-work upload route in this app already uses
    (`api/documents.py::upload_document`, `api/voice.py`) -- parsing/
    resizing/extracting several file types is genuinely non-trivial work.

    Every file is validated and processed independently: one bad file in a
    multi-file upload never aborts the others, and a validation failure is
    reported per-file in `AttachmentUploadResponse.errors` rather than
    failing the whole request with an HTTP error status -- the caller may
    have selected 3 good files and 1 bad one, and the 3 good ones should
    still go through.
    """
    settings = get_settings()
    enforce_api_action_rate_limit(request, "attachment_upload", settings)

    if not settings.enable_chat_attachments:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat attachments are disabled. Set ENABLE_CHAT_ATTACHMENTS=true in .env.",
        )

    owner_subject = _owner_subject(identity)
    store = get_default_attachment_store()

    attachments_out: list[AttachmentOut] = []
    errors_out: list[AttachmentErrorOut] = []
    hashes_seen: set[str] = set()
    total_bytes = 0

    # Read at most the larger of the two per-kind size limits, plus one
    # byte -- mirrors api/documents.py::upload_document's "read-and-reject-
    # if-over" pattern (never trusts Content-Length, never buffers an
    # unbounded stream). attachments.validation.validate_upload applies the
    # real, kind-specific limit against the bytes actually read.
    max_bytes = max(settings.max_attachment_image_bytes, settings.max_attachment_document_bytes)

    for upload in files:
        file_bytes = await upload.read(max_bytes + 1)
        filename = upload.filename or "attachment"

        attachment, validation_result = validate_and_store_upload(
            filename,
            upload.content_type,
            file_bytes,
            settings,
            owner_subject=owner_subject,
            store=store,
            hashes_already_in_this_request=frozenset(hashes_seen),
            current_attachment_count=len(attachments_out),
            current_total_bytes=total_bytes,
        )

        if attachment is not None:
            hashes_seen.add(attachment.sha256)
            total_bytes += attachment.size_bytes
            attachments_out.append(
                AttachmentOut(
                    attachment_id=attachment.attachment_id,
                    filename=attachment.original_filename,
                    media_type=attachment.media_type,
                    size_bytes=attachment.size_bytes,
                    processing_status=attachment.processing_status,
                    processing_error=attachment.processing_error,
                )
            )
        else:
            # Still recorded, so a second identical bad file later in the
            # same batch is caught by the within-request duplicate check
            # too, not just a genuinely-stored one.
            hashes_seen.add(compute_sha256(file_bytes))
            errors_out.extend(
                AttachmentErrorOut(
                    code=error.code.value,
                    attachment_id=error.attachment_id,
                    filename=error.filename,
                    message=error.message,
                )
                for error in validation_result.errors
            )

    return AttachmentUploadResponse(attachments=attachments_out, errors=errors_out)


@router.get("/attachments/capabilities", response_model=AttachmentCapabilitiesResponse)
def get_capabilities(
    _identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> AttachmentCapabilitiesResponse:
    """Returns what this deployment can actually do with an attachment right
    now -- vision/OCR/resize/text-removal availability, size limits, and
    resize presets. Computed fresh from live `Settings` on every call (see
    `attachments.capabilities`'s own docstring for why); the frontend uses
    this to enable/disable each action honestly rather than assuming
    everything configured elsewhere in this file is actually usable."""
    capabilities = get_attachment_capabilities(get_settings())
    return AttachmentCapabilitiesResponse(
        enabled=capabilities.enabled,
        vision_input=capabilities.vision_input,
        vision_model=capabilities.vision_model,
        ocr=capabilities.ocr,
        image_resize=capabilities.image_resize,
        image_text_removal=capabilities.image_text_removal,
        image_text_removal_method=capabilities.image_text_removal_method,
        native_pdf_input=capabilities.native_pdf_input,
        max_image_bytes=capabilities.max_image_bytes,
        max_document_bytes=capabilities.max_document_bytes,
        max_attachments_per_message=capabilities.max_attachments_per_message,
        max_total_attachment_bytes=capabilities.max_total_attachment_bytes,
        max_resize_dimension_px=capabilities.max_resize_dimension_px,
        max_text_removal_regions=capabilities.max_text_removal_regions,
        supported_image_extensions=capabilities.supported_image_extensions,
        supported_document_extensions=capabilities.supported_document_extensions,
        resize_presets=[
            ImageResizePresetOut(name=preset.name, width=preset.width, height=preset.height)
            for preset in capabilities.resize_presets
        ],
    )


def _load_owned_image_bytes(attachment_id: str, owner_subject: str | None):
    """Resolves `attachment_id` to its stored `Attachment` + raw on-disk
    bytes, scoped to `owner_subject` -- shared by every image-action route
    below. Raises the same 404 for "never existed," "not an image," and
    "belongs to another caller" (never distinguishing which, per this
    package's account-enumeration-avoidance posture -- see
    `attachments.store.AttachmentStore`'s own docstring)."""
    store = get_default_attachment_store()
    attachment = store.get(attachment_id, owner_subject=owner_subject)
    if (
        attachment is None
        or attachment.media_type not in IMAGE_MEDIA_TYPES
        or not attachment.local_path
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found.")
    file_bytes = Path(attachment.local_path).read_bytes()
    return attachment, file_bytes


@router.post("/attachments/{attachment_id}/extract-text", response_model=OcrExtractResponse)
def extract_text_route(
    attachment_id: str,
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> OcrExtractResponse:
    """ "Extract text" -- capability (B), OCR only. Deliberately a distinct
    route from asking a vision question about the same image (`POST /ask`
    with `attachment_ids`): this runs Tesseract directly and returns exactly
    what it recognized, never an LLM's paraphrase of the image (see
    `attachments.ocr_extract`'s own docstring)."""
    settings = get_settings()
    enforce_api_action_rate_limit(request, "attachment_extract_text", settings)
    owner_subject = _owner_subject(identity)
    attachment, file_bytes = _load_owned_image_bytes(attachment_id, owner_subject)

    try:
        result = extract_text_with_regions(file_bytes, settings)
    except ImageDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if result.raw_text:
        # Persisted onto the attachment's own extracted_text so a follow-up
        # question in the same conversation ("what did that say?") can see
        # it via the normal attachment-context path -- see
        # attachments.graph.build_attachment_context_node's own comment for
        # why an image is otherwise excluded from that path.
        store = get_default_attachment_store()
        store.put(
            attachment.with_processing_result(
                status=attachment.processing_status,
                extracted_text=result.raw_text,
                image_data_url=attachment.image_data_url,
            )
        )

    return OcrExtractResponse(
        attachment_id=attachment_id,
        raw_text=result.raw_text,
        cleaned_text=result.cleaned_text,
        regions=[
            TextRegionOut(
                text=region.text,
                left=region.left,
                top=region.top,
                width=region.width,
                height=region.height,
                confidence=region.confidence,
                low_confidence=region.low_confidence,
            )
            for region in result.regions
        ],
        warnings=result.warnings,
    )


@router.get(
    "/attachments/{attachment_id}/detect-text-regions", response_model=DetectTextRegionsResponse
)
def detect_text_regions_route(
    attachment_id: str,
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> DetectTextRegionsResponse:
    """Proposes text-line regions for the "Remove text" workflow's
    confirm/adjust step (per this feature's own "automatic detection with
    user confirmation, or manual selection" requirement) -- OCR only, no
    pixels are edited by this route."""
    settings = get_settings()
    enforce_api_action_rate_limit(request, "attachment_detect_text_regions", settings)
    owner_subject = _owner_subject(identity)
    _attachment, file_bytes = _load_owned_image_bytes(attachment_id, owner_subject)

    try:
        regions = detect_text_line_regions(file_bytes, settings)
    except ImageDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return DetectTextRegionsResponse(
        attachment_id=attachment_id,
        regions=[
            TextLineRegionOut(
                region_id=region.region_id,
                text=region.text,
                left=region.left,
                top=region.top,
                width=region.width,
                height=region.height,
                confidence=region.confidence,
            )
            for region in regions
        ],
    )


@router.post("/attachments/{attachment_id}/remove-text", response_model=ImageEditResultResponse)
def remove_text_route(
    attachment_id: str,
    body: RemoveTextRequest,
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> ImageEditResultResponse:
    """ "Remove text" -- capability (D), real pixel editing via classical
    inpainting (`attachments.inpaint`), never a solid rectangle and never
    just hiding text client-side. `body.regions` is either OCR-proposed
    (from `detect_text_regions_route` above, caller-confirmed) or drawn
    manually -- either way, in source-image pixel coordinates. The original
    attachment is untouched; this always produces and stores a new one."""
    settings = get_settings()
    enforce_api_action_rate_limit(request, "attachment_remove_text", settings)
    owner_subject = _owner_subject(identity)
    source, file_bytes = _load_owned_image_bytes(attachment_id, owner_subject)

    try:
        result = remove_text(
            file_bytes,
            settings,
            regions=[
                InpaintRegion(left=r.left, top=r.top, width=r.width, height=r.height)
                for r in body.regions
            ],
        )
    except ImageDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    store = get_default_attachment_store()
    derived = register_derived_image(
        source,
        result.image_bytes,
        result.media_type,
        suffix="text-removed",
        settings=settings,
        store=store,
    )
    return ImageEditResultResponse(
        attachment_id=derived.attachment_id,
        source_attachment_id=attachment_id,
        operation="remove_text",
        image_data_url=derived.image_data_url or "",
        media_type=result.media_type,
        width=result.width,
        height=result.height,
        size_bytes=len(result.image_bytes),
        warnings=result.warnings,
    )


@router.post("/attachments/{attachment_id}/resize", response_model=ImageEditResultResponse)
def resize_route(
    attachment_id: str,
    body: ImageResizeRequest,
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> ImageEditResultResponse:
    """ "Resize" -- capability (C), deterministic Pillow work, no model call.
    Always produces and stores a new attachment (the original is untouched);
    if the caller attaches the returned `attachment_id` to a follow-up
    question, it's the *resized* bytes that reach the model, never the
    original (per this feature's own requirement)."""
    settings = get_settings()
    enforce_api_action_rate_limit(request, "attachment_resize", settings)
    owner_subject = _owner_subject(identity)
    source, file_bytes = _load_owned_image_bytes(attachment_id, owner_subject)

    try:
        result = resize_image(
            file_bytes,
            settings,
            width=body.width,
            height=body.height,
            fit=body.fit,
            output_format=body.output_format,
            quality=body.quality,
        )
    except ImageDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    store = get_default_attachment_store()
    derived = register_derived_image(
        source,
        result.image_bytes,
        result.media_type,
        suffix="resized",
        settings=settings,
        store=store,
    )
    return ImageEditResultResponse(
        attachment_id=derived.attachment_id,
        source_attachment_id=attachment_id,
        operation="resize",
        image_data_url=derived.image_data_url or "",
        media_type=result.media_type,
        width=result.width,
        height=result.height,
        original_width=result.original_width,
        original_height=result.original_height,
        size_bytes=result.size_bytes,
        warnings=[],
    )


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_attachment_route(
    attachment_id: str,
    identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
) -> None:
    """Removes an attachment (registry entry + its on-disk bytes) -- backs
    the composer's per-attachment "remove" button.

    Scoped to the caller's own attachments only (`attachments.store
    .AttachmentStore`'s ownership check, via `_owner_subject`) -- a request
    for an id that exists but belongs to a different caller resolves to the
    same 404 as an id that was never stored at all, never confirming
    another user's attachment even exists (the same account-enumeration-
    avoidance shape `identity.exceptions.InvalidCredentialsError` already
    uses for login).
    """
    owner_subject = _owner_subject(identity)
    store = get_default_attachment_store()
    deleted = delete_attachment(attachment_id, owner_subject=owner_subject, store=store)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found.")
