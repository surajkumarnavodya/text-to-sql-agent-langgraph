"""Custom exceptions for the media-search layer.

Same two-message convention as `agent/exceptions.py::AgentError`/
`voice/exceptions.py::VoiceError` (not a subclass of either -- media search
isn't agent-graph logic, it's a standalone capability
`agent.orchestrator.nodes.media_search_node`, `api/media_search.py`, and
`api/media_library.py` call into): `str(exc)` is the full internal detail
for logs, `.safe_message` is a short, non-technical sentence safe to put
directly in an API response.
"""

from __future__ import annotations

_DEFAULT_SAFE_MESSAGE = "Media search is temporarily unavailable. Please try again."


class MediaSearchError(Exception):
    """Base class for all media-search-layer errors."""

    _default_safe_message: str = _DEFAULT_SAFE_MESSAGE

    def __init__(self, detail: str, *, safe_message: str | None = None) -> None:
        super().__init__(detail)
        self.safe_message = safe_message or self._default_safe_message


class MediaSearchNotConfiguredError(MediaSearchError):
    """Raised when media search is used without both `ENABLE_MEDIA_SEARCH`
    and a real `MEDIA_LIBRARY_PATH` set."""

    _default_safe_message = "Media search is not configured."


class MediaEmbeddingModelNotFoundError(MediaSearchError):
    """Raised when the configured CLIP model can't be loaded (no network on
    first use, corrupt local cache, or an invalid model name)."""

    _default_safe_message = (
        "The media embedding model isn't available yet. It downloads "
        "automatically on first use -- check network access and try again."
    )


class UnsupportedMediaTypeError(MediaSearchError):
    """Raised when a file's actual content (checked via magic bytes, never
    just its extension) doesn't match a supported image/video type."""

    _default_safe_message = "That file type isn't supported for media search."


class MediaFileTooLargeError(MediaSearchError):
    """Raised when a file exceeds `Settings.media_max_file_mb`."""

    _default_safe_message = "That file is too large to index."
