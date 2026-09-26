"""Top-level orchestration: validate -> malware-scan -> store -> process ->
register, and the `Attachment` <-> `ProcessedAttachment` conversions every
other layer (the API, the LangGraph subgraph) builds on.

Processing happens once, eagerly, at upload time (`validate_and_store_upload`)
-- not lazily when a question later references the attachment. This is what
makes a follow-up question ("what's the total in that spreadsheet I
uploaded earlier?") free: `attachments.store.AttachmentStore` already holds
the fully-processed record, so `attachments.graph`'s own `process_attachments`
node is a cheap dict lookup + conversion, never a re-parse, unless the
record is somehow still `"pending"` (defensive only -- shouldn't happen on
the normal upload path).
"""

from __future__ import annotations

import concurrent.futures
import logging
from functools import cache
from pathlib import Path

from attachments.image_processing import image_bytes_to_data_url, normalize_image
from attachments.models import (
    Attachment,
    AttachmentError,
    AttachmentErrorCode,
    ProcessedAttachment,
    ValidationResult,
)
from attachments.processors.base import ProcessingOutcome
from attachments.processors.registry import get_processor_for
from attachments.storage import (
    delete_attachment_file,
    purge_expired_attachments,
    sanitize_filename,
    save_attachment_bytes,
)
from attachments.store import AttachmentStore, get_default_attachment_store
from attachments.validation import EXTENSION_TO_MEDIA_TYPE, compute_sha256, validate_upload
from config.settings import Settings
from security.audit_log import log_security_event
from security.injection_patterns import INJECTION_PATTERNS
from security.malware_scanner import scan_upload

logger = logging.getLogger(__name__)

_IMAGE_MEDIA_TYPE_TO_EXTENSION = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class AttachmentProcessingTimeoutError(Exception):
    """Raised when a processor's `process()` call exceeds
    `Settings.attachment_processing_timeout_seconds`. Same "the calling
    thread stops waiting; the abandoned call keeps running inside the
    bounded pool" caveat `api/main.py::_run_orchestrated_with_timeout`
    already documents for `/ask` -- Python has no safe, universal way to
    force-kill another thread, so this bounds how long a request *waits*
    for a pathological file, not the CPU time the runaway call itself
    eventually consumes.
    """


@cache
def _get_processing_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Process-wide bounded thread pool for attachment processor calls --
    mirrors `api/main.py::_get_ask_executor`'s own reasoning: a fixed pool
    size means an abandoned (timed-out) task occupies one of a *bounded*
    number of slots rather than an unbounded extra OS thread piling up
    under repeated pathological uploads. `@cache`, the same process-
    lifetime-singleton pattern every other cached-until-restart resource in
    this codebase already uses."""
    return concurrent.futures.ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="attachment-proc"
    )


def _run_processor_with_timeout(
    processor, attachment: Attachment, settings: Settings
) -> ProcessingOutcome:  # noqa: ANN001 - FileProcessor Protocol
    future = _get_processing_executor().submit(processor.process, attachment, settings)
    try:
        return future.result(timeout=settings.attachment_processing_timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        raise AttachmentProcessingTimeoutError(
            f"Processing timed out after {settings.attachment_processing_timeout_seconds:.0f}s"
        ) from exc


def _scan_for_injection_patterns(attachment: Attachment, extracted_text: str) -> None:
    """Detection-only prompt-injection risk signal over an attachment's own
    extracted text -- never blocks (the structural "untrusted data, never
    instructions" framing in `attachments/graph.py`'s system prompts is
    what actually bounds the consequence if this fires for real), mirrors
    `rag/ingestion.py::ingest_pdf`'s identical "log a security event, keep
    processing" posture for the persistent Knowledge Sources pipeline --
    this closes the one real gap that pipeline's own scan didn't cover:
    chat attachments never ran this check at all before now, regardless of
    file type (PDF, DOCX, XLSX, PPTX, or OCR'd image text).
    """
    matched_patterns = [
        name for name, pattern in INJECTION_PATTERNS.items() if pattern.search(extracted_text)
    ]
    if not matched_patterns:
        return
    logger.warning(
        "[attachments] [possible_prompt_injection] attachment_id=%s matched pattern(s): %s "
        "-- proceeding (detection only; untrusted-data framing is the real boundary)",
        attachment.attachment_id,
        matched_patterns,
    )
    log_security_event(
        "possible_attachment_injection",
        "warning",
        "Extracted attachment text matched an injection-style pattern before being "
        "used as prompt context.",
        attachment_id=attachment.attachment_id,
        filename=attachment.original_filename,
        owner_subject=attachment.owner_subject,
        matched_patterns=matched_patterns,
    )


def process_attachment(attachment: Attachment, settings: Settings) -> Attachment:
    """Runs the matching `FileProcessor` (bounded by
    `Settings.attachment_processing_timeout_seconds`) and returns a new
    `Attachment` with the outcome applied.

    Never raises -- an unexpected processor exception (a genuine bug, a
    disk I/O error reading `local_path`), and a processor that overruns its
    time budget, are both caught here and converted into a structured
    failed outcome, so one bad or pathological file can never crash or hang
    the request that uploaded it or a later question that references it.
    """
    processor = get_processor_for(attachment)
    if processor is None:
        return attachment.with_processing_result(
            status="unsupported",
            error=f"No processor is available for {attachment.media_type!r}.",
        )
    try:
        outcome = _run_processor_with_timeout(processor, attachment, settings)
    except AttachmentProcessingTimeoutError:
        logger.warning(
            "[attachments] processor %s timed out for attachment_id=%s",
            type(processor).__name__,
            attachment.attachment_id,
        )
        return attachment.with_processing_result(
            status="failed",
            error=(
                "Processing this file took too long and was stopped. Try a smaller "
                "or simpler file."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a processor bug must not crash the request
        logger.exception(
            "[attachments] processor %s failed unexpectedly for attachment_id=%s",
            type(processor).__name__,
            attachment.attachment_id,
        )
        return attachment.with_processing_result(
            status="failed", error=f"Processing failed unexpectedly: {exc}"
        )

    if outcome.status == "succeeded" and outcome.extracted_text:
        _scan_for_injection_patterns(attachment, outcome.extracted_text)

    logger.info(
        "[attachments] processed attachment_id=%s filename=%r status=%s "
        "extracted_chars=%d has_image=%s",
        attachment.attachment_id,
        attachment.original_filename,
        outcome.status,
        len(outcome.extracted_text or ""),
        bool(outcome.image_data_url),
    )
    return attachment.with_processing_result(
        status=outcome.status,
        extracted_text=outcome.extracted_text,
        extracted_tables=outcome.extracted_tables,
        extracted_metadata=outcome.metadata,
        image_data_url=outcome.image_data_url,
        error=outcome.error,
    )


def source_type_for(attachment: Attachment) -> str:
    """The short label (`"pdf"`, `"image"`, `"docx"`, ...) a processor
    identifies itself with -- looked up from the registry rather than
    duplicated as a second media-type mapping."""
    processor = get_processor_for(attachment)
    return processor.source_type if processor is not None else "unknown"


def to_processed_attachment(attachment: Attachment) -> ProcessedAttachment:
    """Converts a stored `Attachment` into the plain-dict shape LangGraph
    state actually holds -- see `attachments.models`'s module docstring for
    why these are two different shapes."""
    return ProcessedAttachment(
        attachment_id=attachment.attachment_id,
        filename=attachment.original_filename,
        media_type=attachment.media_type,
        source_type=source_type_for(attachment),
        extracted_text=attachment.extracted_text or "",
        image_data_url=attachment.image_data_url or "",
        chunks=[],
        metadata=attachment.extracted_metadata or {},
        processing_status=attachment.processing_status,
        error=attachment.processing_error or "",
    )


def validate_and_store_upload(
    filename: str,
    declared_media_type: str | None,
    file_bytes: bytes,
    settings: Settings,
    *,
    owner_subject: str | None = None,
    store: AttachmentStore | None = None,
    hashes_already_in_this_request: frozenset[str] = frozenset(),
    current_attachment_count: int = 0,
    current_total_bytes: int = 0,
) -> tuple[Attachment | None, ValidationResult]:
    """Validates, malware-scans, stores, and eagerly processes one uploaded
    file -- the single entry point `api/attachments.py`'s upload endpoint
    calls per file.

    Returns:
        `(attachment, validation_result)` -- `attachment` is `None` whenever
        `validation_result.valid` is `False`; otherwise it's the final,
        already-processed record (its own `processing_status`/
        `processing_error` reflect whether extraction itself succeeded --
        that's a separate outcome from upload validation succeeding, per
        this feature's "preserve the failure in state" requirement rather
        than rejecting the whole upload).
    """
    store = store or get_default_attachment_store()

    validation_result = validate_upload(
        filename,
        declared_media_type,
        file_bytes,
        settings,
        hashes_already_in_this_request=hashes_already_in_this_request,
        current_attachment_count=current_attachment_count,
        current_total_bytes=current_total_bytes,
    )
    if not validation_result.valid:
        return None, validation_result

    file_hash = compute_sha256(file_bytes)

    # Malware scan on the raw bytes, before any parser touches them --
    # mirrors rag/ingestion.py::ingest_pdf's identical ordering rationale
    # (feeding attacker-crafted bytes into pypdf/python-docx/openpyxl/
    # python-pptx/Pillow is itself part of this app's attack surface,
    # independent of whether the parsed content would also be a problem).
    # Off by default (Settings.malware_scan_provider == "disabled") --
    # see that setting's own docstring for why "disabled" is a deliberate
    # default, not an oversight.
    scan_result = scan_upload(file_hash, file_bytes, settings)
    if scan_result.blocked:
        error = AttachmentError(
            code=AttachmentErrorCode.MALWARE_DETECTED,
            filename=filename,
            message="This file was rejected because it could not be verified as safe.",
        )
        return None, ValidationResult(valid=False, errors=[error])

    existing = store.get_by_hash(file_hash, owner_subject=owner_subject)
    if existing is not None and existing.processing_status in (
        "succeeded",
        "unsupported",
        "failed",
    ):
        # Reuse rather than reprocess -- see this module's docstring.
        # A previously-failed identical upload is also reused as-is (its own
        # processing_error is preserved) rather than retried, since nothing
        # about the file's bytes changed and retrying would just fail again
        # identically -- the same "deterministic and reusable" contract
        # this feature asked for applies to a failure outcome too.
        purge_expired_attachments(settings)
        return existing, ValidationResult(
            valid=True,
            attachment_id=existing.attachment_id,
            warnings=[*validation_result.warnings, "Reused a previously uploaded identical file."],
        )

    extension = Path(filename).suffix.lower()
    media_type = EXTENSION_TO_MEDIA_TYPE[extension]

    attachment = Attachment(
        original_filename=filename,
        safe_filename=sanitize_filename(filename),
        media_type=media_type,
        extension=extension,
        size_bytes=len(file_bytes),
        sha256=file_hash,
        owner_subject=owner_subject,
    )

    try:
        saved_path = save_attachment_bytes(
            settings, attachment.attachment_id, extension, file_bytes
        )
    except OSError:
        logger.exception(
            "[attachments] failed to save attachment_id=%s to disk", attachment.attachment_id
        )
        error = AttachmentError(
            code=AttachmentErrorCode.STORAGE_FAILED,
            filename=filename,
            message="This file could not be saved. Please try again.",
        )
        return None, ValidationResult(valid=False, errors=[error])

    attachment = attachment.model_copy(update={"local_path": str(saved_path)})
    store.put(attachment)

    processed = process_attachment(attachment, settings)
    store.put(processed)

    purge_expired_attachments(settings)

    return processed, ValidationResult(
        valid=True, attachment_id=processed.attachment_id, warnings=validation_result.warnings
    )


def delete_attachment(
    attachment_id: str, *, owner_subject: str | None = None, store: AttachmentStore | None = None
) -> bool:
    """Removes an attachment -- backs `DELETE /attachments/{id}`'s "Remove"
    button support. Returns whether anything was actually deleted (a
    caller-facing 404 vs. 204 distinction the API layer makes)."""
    store = store or get_default_attachment_store()
    return store.delete(attachment_id, owner_subject=owner_subject)


def resolve_processed_attachments(
    attachment_ids: list[str],
    *,
    owner_subject: str | None = None,
    settings: Settings | None = None,
    store: AttachmentStore | None = None,
) -> tuple[list[ProcessedAttachment], list[str]]:
    """Resolves `attachment_ids` to their processed content -- the function
    `attachments.graph`'s `process_attachments_node` and
    `agent.orchestrator.nodes.attachment_node` both call.

    A resolved attachment still `"pending"` (shouldn't happen given
    `validate_and_store_upload` always processes eagerly, but handled
    defensively rather than assumed) is processed here on demand before
    being converted, so a caller never sees an unprocessed record.

    Returns:
        `(processed, missing_ids)` -- see `AttachmentStore.resolve_many`'s
        own docstring for what "missing" covers (never found, evicted, or
        not owned by this caller -- indistinguishable to the caller, per
        this module's access-control posture).
    """
    from config.settings import get_settings

    store = store or get_default_attachment_store()
    settings = settings or get_settings()

    found, missing = store.resolve_many(attachment_ids, owner_subject=owner_subject)
    processed: list[ProcessedAttachment] = []
    for attachment in found:
        if attachment.processing_status == "pending":
            attachment = process_attachment(attachment, settings)
            store.put(attachment)
        processed.append(to_processed_attachment(attachment))
    return processed, missing


def register_derived_image(
    source: Attachment,
    image_bytes: bytes,
    media_type: str,
    *,
    suffix: str,
    settings: Settings,
    store: AttachmentStore | None = None,
) -> Attachment:
    """Stores the output of an image action (resize, text removal) as a
    brand-new `Attachment` -- never mutates `source` in place, so the
    original stays available exactly per this feature's own "keep the
    original unless the user explicitly deletes it" requirement.

    The new attachment is immediately usable two ways: `attachment_id` can
    be attached to a follow-up `/ask` question (so the *edited* bytes, not
    the original, are what reaches the model), and the API response also
    returns a data URL for an immediate download/preview -- see
    `api/attachments.py`'s resize/remove-text routes.

    Args:
        source: The attachment this output was derived from -- only its
            filename (for a readable derived name) and `owner_subject`
            (carried over so ownership scoping still applies) are reused.
        image_bytes: The new, already-encoded image bytes.
        media_type: `image_bytes`'s media type (`"image/png"`/`"image/jpeg"`/
            `"image/webp"`).
        suffix: Appended to the source filename's stem, e.g. `"resized"` or
            `"text-removed"`.
    """
    store = store or get_default_attachment_store()
    extension = _IMAGE_MEDIA_TYPE_TO_EXTENSION.get(media_type, ".png")
    stem = Path(source.original_filename).stem or "image"
    filename = f"{stem}_{suffix}{extension}"

    attachment = Attachment(
        original_filename=filename,
        safe_filename=sanitize_filename(filename),
        media_type=media_type,
        extension=extension,
        size_bytes=len(image_bytes),
        sha256=compute_sha256(image_bytes),
        owner_subject=source.owner_subject,
    )
    saved_path = save_attachment_bytes(settings, attachment.attachment_id, extension, image_bytes)
    attachment = attachment.model_copy(update={"local_path": str(saved_path)})

    # A model-safe data URL (downscaled/re-encoded per max_attachment_image_
    # dimension_px, same as any other image attachment) -- independent of
    # `image_bytes` itself, which may be larger (a resize's whole point can
    # be producing an output bigger than the vision-prompt ceiling).
    normalized_bytes, normalized_media_type = normalize_image(image_bytes, settings)
    data_url = image_bytes_to_data_url(normalized_bytes, normalized_media_type)

    processed = attachment.with_processing_result(status="succeeded", image_data_url=data_url)
    store.put(processed)
    return processed


def cleanup_attachment_disk_file(attachment: Attachment) -> None:
    """Best-effort disk cleanup for an attachment that failed after its
    bytes were already written (used by tests and any future explicit
    rollback path) -- exposed separately from `attachments.store
    .AttachmentStore.delete` since a caller may need to clean up a file
    that was never registered in the store at all."""
    if attachment.local_path:
        delete_attachment_file(attachment.local_path)
