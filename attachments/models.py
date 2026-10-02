"""Data models for the chat-attachment pipeline.

Two related but distinct shapes, deliberately not merged into one:

- `Attachment` -- the canonical, persisted-in-`attachments.store` record for
  one uploaded file. Frozen (`pydantic.BaseModel`, matching every other
  boundary model in this codebase -- see CLAUDE.md's "Pydantic-based
  configuration and validation" section) -- updated via
  `with_processing_result`, which returns a *new* instance rather than
  mutating in place, the same "frozen model + explicit replace" convention
  `agent.sql_validator.ValidationResult` already follows.
- `ProcessedAttachment` -- a plain `TypedDict`, the shape actually embedded
  into LangGraph state (`attachments.state.AttachmentQAState`,
  `agent.orchestrator.state.OrchestratorState`). Deliberately a bare dict,
  never the `Attachment` model itself -- the same "TypedDict state holds
  plain dicts, not model instances" convention `agent.state.TableSchema`/
  `GoldenExample` already follow, so LangGraph's own state-merging (plain
  dict updates) never has to know about Pydantic.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# typing_extensions.TypedDict (not stdlib typing.TypedDict) is required here:
# ProcessedAttachment is a field type on attachments.state.AttachmentQAState,
# a LangGraph node's state, and LangGraph's StateGraph.get_graph() triggers
# pydantic v2 to build a schema for it -- which raises PydanticUserError on
# Python < 3.12, where stdlib TypedDict lacks __orig_bases__ (added in
# CPython 3.12). Production runs Python 3.11.
from typing_extensions import TypedDict

# Every kind this pipeline can validate/store, independent of whether a
# processor exists yet for it -- see attachments.validation for the actual
# MIME/extension allowlists these map to.
AttachmentKind = (
    str  # "image" | "pdf" | "text" | "markdown" | "json" | "csv" | "docx" | "xlsx" | "pptx"
)

ProcessingStatus = str  # "pending" | "processing" | "succeeded" | "failed" | "unsupported"


class AttachmentErrorCode(str, Enum):
    """Stable machine-readable codes -- see this module's docstring and
    CLAUDE.md's "Centralized exception handling" section for why a stable
    code (not just prose) matters for a client to branch on."""

    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    TOTAL_SIZE_TOO_LARGE = "TOTAL_SIZE_TOO_LARGE"
    TOO_MANY_ATTACHMENTS = "TOO_MANY_ATTACHMENTS"
    EMPTY_FILE = "EMPTY_FILE"
    CORRUPTED_IMAGE = "CORRUPTED_IMAGE"
    CORRUPTED_DOCUMENT = "CORRUPTED_DOCUMENT"
    PASSWORD_PROTECTED = "PASSWORD_PROTECTED"
    OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
    INVALID_OFFICE_DOCUMENT = "INVALID_OFFICE_DOCUMENT"
    MODEL_NO_IMAGE_SUPPORT = "MODEL_NO_IMAGE_SUPPORT"
    CONTEXT_TRUNCATED = "CONTEXT_TRUNCATED"
    ATTACHMENT_NOT_FOUND = "ATTACHMENT_NOT_FOUND"
    UPLOAD_INTERRUPTED = "UPLOAD_INTERRUPTED"
    DUPLICATE_FILE = "DUPLICATE_FILE"
    MALICIOUS_FILENAME = "MALICIOUS_FILENAME"
    INVALID_ENCODING = "INVALID_ENCODING"
    MALWARE_DETECTED = "MALWARE_DETECTED"
    ACCESS_DENIED = "ACCESS_DENIED"
    STORAGE_FAILED = "STORAGE_FAILED"
    PROCESSING_FAILED = "PROCESSING_FAILED"


class AttachmentError(BaseModel):
    """One structured, user-friendly error -- see CLAUDE.md's "Centralized
    exception handling" section for the same two-audiences split (a stable
    code + short user-facing message here; full diagnostic detail stays in
    server logs only, via `logging`, never in this model)."""

    model_config = ConfigDict(frozen=True)

    code: AttachmentErrorCode
    attachment_id: str | None = None
    filename: str
    message: str


def _new_attachment_id() -> str:
    # A short, sortable, collision-resistant id -- time-prefixed hex, not a
    # UUID: this ties naturally into `attachments.storage.
    # purge_expired_attachments`'s "oldest first" sweep without needing a
    # separate created_at index, while still being effectively unguessable
    # per-process (the random suffix). uuid4 would work equally well; this
    # is a deliberate, minor convenience choice, not a security boundary.
    import secrets

    return f"att_{int(time.time())}_{secrets.token_hex(8)}"


class Attachment(BaseModel):
    """The canonical record for one uploaded file -- see this module's
    docstring for how this differs from `ProcessedAttachment`.

    Never trust `original_filename` for anything beyond display -- `
    safe_filename` (see `attachments.storage.sanitize_filename`) is what's
    actually used on disk, and `attachment_id` (never the filename) is what
    every downstream reference (LangGraph state, the API, conversation
    history) keys on.
    """

    model_config = ConfigDict(frozen=True)

    attachment_id: str = Field(default_factory=_new_attachment_id)
    original_filename: str
    safe_filename: str
    media_type: str
    extension: str
    size_bytes: int
    sha256: str
    local_path: str | None = None
    preview_url: str | None = None
    extracted_text: str | None = None
    extracted_tables: list[dict[str, Any]] | None = None
    extracted_metadata: dict[str, Any] | None = None
    image_data_url: str | None = None
    processing_status: ProcessingStatus = "pending"
    processing_error: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    # Not in the spec's original field list -- added for the "one user
    # cannot access another user's attachment" requirement (CLAUDE.md-style
    # security posture this codebase applies everywhere else). None for an
    # unauthenticated/"none"/"static_token"-auth-mode caller, matching
    # `agent.state.AgentState.caller_subject`'s own "no real per-user
    # identity exists" semantics -- an attachment with no owner is treated
    # as accessible to any caller in that mode, since there is no identity
    # to scope it to in the first place.
    owner_subject: str | None = None

    def with_processing_result(
        self,
        *,
        status: ProcessingStatus,
        extracted_text: str | None = None,
        extracted_tables: list[dict[str, Any]] | None = None,
        extracted_metadata: dict[str, Any] | None = None,
        image_data_url: str | None = None,
        error: str | None = None,
    ) -> Attachment:
        """Returns a new `Attachment` with processing results applied --
        frozen models are never mutated in place, matching
        `agent.sql_validator.ValidationResult`'s own convention."""
        return self.model_copy(
            update={
                "processing_status": status,
                "extracted_text": extracted_text,
                "extracted_tables": extracted_tables,
                "extracted_metadata": extracted_metadata,
                "image_data_url": image_data_url,
                "processing_error": error,
            }
        )


class ProcessedAttachment(TypedDict, total=False):
    """The LangGraph-state-embeddable shape -- see this module's docstring.

    Built from an `Attachment` via `attachments.pipeline.to_processed_attachment`
    once processing has run; never constructed directly by a node.
    """

    attachment_id: str
    filename: str
    media_type: str
    source_type: str
    extracted_text: str
    image_data_url: str
    chunks: list[dict[str, Any]]
    metadata: dict[str, Any]
    processing_status: str
    error: str


class ValidationResult(BaseModel):
    """Backs both `attachments.validation.validate_upload`'s return value and
    `POST /attachments/upload`'s per-file response entry -- see this
    module's docstring for the exact shape the spec asked for."""

    model_config = ConfigDict(frozen=True)

    valid: bool
    attachment_id: str | None = None
    errors: list[AttachmentError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
