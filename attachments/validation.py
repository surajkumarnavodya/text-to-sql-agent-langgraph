"""Validates one candidate chat attachment before anything is stored.

Every check here is a pure function of already-in-memory bytes/metadata --
no disk writes, no network calls -- so it's cheap to run before spending any
real work on a file that's going to be rejected anyway. Mirrors
`api/documents.py::upload_document`'s existing "read-and-reject-if-over,
magic-byte-check before trusting the extension" posture, generalized across
every attachment kind this pipeline supports.

Extension AND declared MIME type are both checked (per this feature's own
requirement) -- extension is what actually decides which processor runs
(`attachments.processors.registry`), since a client-reported `Content-Type`
is exactly as spoofable as `frontend/src/lib/imageValidation.ts`'s own
docstring already documents for images. A declared MIME type that disagrees
with the extension-derived one is a warning, not a hard rejection, since a
mismatch there is more often a mislabeling browser/client than an actual
attack -- the file signature check below is what actually catches a
disguised payload.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from attachments.models import AttachmentError, AttachmentErrorCode, ValidationResult
from config.settings import Settings

IMAGE_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

DOCUMENT_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "application/json",
        "text/csv",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    }
)

ALLOWED_MEDIA_TYPES: frozenset[str] = IMAGE_MEDIA_TYPES | DOCUMENT_MEDIA_TYPES

# Extension is the authoritative signal for "which processor runs" -- never
# the declared/sniffed MIME type alone. Deliberately excludes .doc/.ppt/.xls
# (legacy binary Office formats): none of attachments/processors/ can parse
# those (python-docx/openpyxl/python-pptx all require the modern OOXML
# (.docx/.xlsx/.pptx) format), so accepting the extension here would let a
# file through validation only to fail at the processing stage with a less
# helpful error. Rejected up front instead, with a message naming the
# actually-supported extension.
EXTENSION_TO_MEDIA_TYPE: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

_LEGACY_OFFICE_EXTENSIONS: dict[str, str] = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
}

# Magic-byte signatures checked against the file's own leading bytes --
# never the extension or declared MIME type alone, mirroring
# frontend/src/lib/imageValidation.ts's MAGIC_BYTES table (client-side
# convenience there; this is the real server-side security boundary).
# DOCX/XLSX/PPTX are all ZIP containers (PK\x03\x04) -- the same signature
# for all three, so this only proves "this is a ZIP," not "this is
# specifically a valid DOCX" -- attachments.processors.* itself is what
# proves the internal OOXML structure is well-formed.
_SIGNATURES: dict[str, list[bytes]] = {
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/gif": [b"GIF8"],
    "application/pdf": [b"%PDF-"],
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [b"PK\x03\x04"],
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [b"PK\x03\x04"],
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": [b"PK\x03\x04"],
}


def _sniff_webp(head: bytes) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _signature_matches(media_type: str, file_bytes: bytes) -> bool:
    """Whether `file_bytes` starts with a signature known for `media_type`.

    Returns True for a media type with no reliable magic-byte signature
    (plain text/Markdown/JSON/CSV) -- there's nothing meaningful to sniff
    for those; encoding/parse validity is checked by the processor instead
    (see `attachments.processors.text_processor`/`json_processor`).
    """
    if media_type == "image/webp":
        return _sniff_webp(file_bytes[:16])
    signatures = _SIGNATURES.get(media_type)
    if signatures is None:
        return True
    return any(file_bytes.startswith(sig) for sig in signatures)


def compute_sha256(file_bytes: bytes) -> str:
    """SHA-256 of `file_bytes` -- the duplicate-detection and
    dedup-across-messages key, mirroring `rag/ingestion.py::_content_hash`'s
    identical pattern for PDF uploads."""
    return hashlib.sha256(file_bytes).hexdigest()


def validate_upload(
    filename: str,
    declared_media_type: str | None,
    file_bytes: bytes,
    settings: Settings,
    *,
    hashes_already_in_this_request: frozenset[str] = frozenset(),
    current_attachment_count: int = 0,
    current_total_bytes: int = 0,
) -> ValidationResult:
    """Validates one candidate attachment. Never raises -- every failure
    mode becomes a structured `AttachmentError` in the returned
    `ValidationResult.errors` instead.

    Args:
        filename: The caller's original filename (untrusted -- used only to
            derive the extension and for error messages; never used as a
            storage path, see `attachments.storage.sanitize_filename`).
        declared_media_type: The client-reported `Content-Type`, if any.
        file_bytes: The full raw upload bytes.
        settings: Current process settings (size/count limits).
        hashes_already_in_this_request: SHA-256 hashes of every attachment
            already validated earlier in the *same* upload call -- catches
            a user selecting the identical file twice in one message
            (`AttachmentErrorCode.DUPLICATE_FILE`). Cross-message/cross-
            conversation reuse of an identical file is NOT rejected here --
            that's handled by `attachments.pipeline.process_attachment`
            reusing the already-processed record instead of re-running
            extraction, per this feature's "don't reprocess unless the file
            changed" requirement.
        current_attachment_count: How many attachments this same upload
            request has already accepted -- enforces
            `Settings.max_attachments_per_message`.
        current_total_bytes: Combined size of every attachment already
            accepted in this same upload request -- enforces
            `Settings.max_total_attachment_bytes`.
    """
    errors: list[AttachmentError] = []
    warnings: list[str] = []

    if current_attachment_count >= settings.max_attachments_per_message:
        errors.append(
            AttachmentError(
                code=AttachmentErrorCode.TOO_MANY_ATTACHMENTS,
                filename=filename,
                message=(
                    f"This message already has {settings.max_attachments_per_message} "
                    "attachment(s), the maximum allowed."
                ),
            )
        )
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    if len(file_bytes) == 0:
        errors.append(
            AttachmentError(
                code=AttachmentErrorCode.EMPTY_FILE,
                filename=filename,
                message="This file is empty and cannot be attached.",
            )
        )
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    extension = Path(filename).suffix.lower()
    if extension in _LEGACY_OFFICE_EXTENSIONS:
        errors.append(
            AttachmentError(
                code=AttachmentErrorCode.UNSUPPORTED_FILE_TYPE,
                filename=filename,
                message=(
                    f"The legacy {extension} format isn't supported -- please save this "
                    f"file as {_LEGACY_OFFICE_EXTENSIONS[extension]} and try again."
                ),
            )
        )
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    media_type = EXTENSION_TO_MEDIA_TYPE.get(extension)
    if media_type is None:
        errors.append(
            AttachmentError(
                code=AttachmentErrorCode.UNSUPPORTED_FILE_TYPE,
                filename=filename,
                message=f"Files of type {extension or '(no extension)'!r} aren't supported.",
            )
        )
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    if (
        declared_media_type
        and declared_media_type in ALLOWED_MEDIA_TYPES
        and declared_media_type != media_type
    ):
        warnings.append(
            f"Declared content type {declared_media_type!r} didn't match the "
            f"{extension!r} extension; treated as {media_type!r}."
        )

    size_limit = (
        settings.max_attachment_image_bytes
        if media_type in IMAGE_MEDIA_TYPES
        else settings.max_attachment_document_bytes
    )
    if len(file_bytes) > size_limit:
        errors.append(
            AttachmentError(
                code=AttachmentErrorCode.FILE_TOO_LARGE,
                filename=filename,
                message=f"This file exceeds the {size_limit // (1024 * 1024)}MB size limit.",
            )
        )
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    if current_total_bytes + len(file_bytes) > settings.max_total_attachment_bytes:
        errors.append(
            AttachmentError(
                code=AttachmentErrorCode.TOTAL_SIZE_TOO_LARGE,
                filename=filename,
                message=(
                    "Adding this file would exceed the "
                    f"{settings.max_total_attachment_bytes // (1024 * 1024)}MB combined "
                    "attachment limit for one message."
                ),
            )
        )
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    if not _signature_matches(media_type, file_bytes):
        error_code = (
            AttachmentErrorCode.CORRUPTED_IMAGE
            if media_type in IMAGE_MEDIA_TYPES
            else AttachmentErrorCode.CORRUPTED_DOCUMENT
        )
        errors.append(
            AttachmentError(
                code=error_code,
                filename=filename,
                message=(
                    f"This file's contents don't match a valid {extension} file -- it "
                    "may be corrupted or renamed from a different format."
                ),
            )
        )
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    file_hash = compute_sha256(file_bytes)
    if file_hash in hashes_already_in_this_request:
        errors.append(
            AttachmentError(
                code=AttachmentErrorCode.DUPLICATE_FILE,
                filename=filename,
                message="This exact file was already attached to this message.",
            )
        )
        return ValidationResult(valid=False, errors=errors, warnings=warnings)

    return ValidationResult(valid=True, errors=[], warnings=warnings)
