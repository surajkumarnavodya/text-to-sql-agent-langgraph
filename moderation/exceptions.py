"""Custom exceptions for the pre-ingestion content-moderation gate.

Same two-message convention as `agent/exceptions.py::AgentError`/
`media/exceptions.py::MediaSearchError` (not a subclass of either --
moderation is a standalone capability both `media/ingest.py` and
`rag/ingestion.py` call into, not agent-graph or media-search-specific
logic): `str(exc)` is the full internal detail for logs, `.safe_message` is
a short, non-technical sentence safe to put directly in an API response.
"""

from __future__ import annotations

_DEFAULT_SAFE_MESSAGE = "Content moderation is temporarily unavailable. Please try again."


class ModerationError(Exception):
    """Base class for all moderation-gate errors."""

    _default_safe_message: str = _DEFAULT_SAFE_MESSAGE

    def __init__(self, detail: str, *, safe_message: str | None = None) -> None:
        super().__init__(detail)
        self.safe_message = safe_message or self._default_safe_message


class ModerationNotConfiguredError(ModerationError):
    """Raised when content is ingested (`media/ingest.py`, `rag/ingestion.py`)
    without the moderation provider and/or metadata store configured.

    There is deliberately no `ENABLE_CONTENT_MODERATION` flag to catch --
    this gate is mandatory whenever `ENABLE_MEDIA_SEARCH` or
    `ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG` is on, so missing
    configuration fails ingestion closed rather than silently skipping the
    gate, mirroring `rag.store.RagStoreNotConfiguredError`'s exact posture.
    """

    _default_safe_message = "Content moderation is not configured."
