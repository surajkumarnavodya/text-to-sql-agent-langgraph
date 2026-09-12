"""Fetches a generated asset's bytes from the provider's CDN, once.

`generation_node` calls this immediately after IMA reports a generation as
complete, so the raw provider URL is only ever dereferenced server-side --
it's never stored in `AgentState`, the API response, or shown to the user
(see `media_gen/cache.py` and `agent.orchestrator.state
.MediaGenerationResult.media_id`).
"""

from __future__ import annotations

import requests

from media_gen.client import MediaGenerationError
from security.redaction import redact_secrets

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def download_media_bytes(url: str, timeout: float = 30.0) -> tuple[bytes, str]:
    """Downloads `url` and returns `(content, content_type)`.

    `content_type` is read from the response's own `Content-Type` header
    (falling back to a generic binary type if absent) -- IMA's CDN, not
    this app, is the source of truth for what kind of file it actually
    served. Raises `MediaGenerationError` on any transport/HTTP failure,
    matching `media_gen.client.IMAClient._request`'s own error contract so
    callers only need to catch one exception type across the whole
    generate-then-download flow.
    """
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise MediaGenerationError(
            f"Network error downloading generated media: {redact_secrets(str(exc))}"
        ) from exc

    if response.status_code >= 400:
        raise MediaGenerationError(
            f"HTTP error {response.status_code} downloading generated media from provider",
            status_code=response.status_code,
        )

    content_type = response.headers.get("Content-Type", _DEFAULT_CONTENT_TYPE).split(";")[0].strip()
    return response.content, content_type or _DEFAULT_CONTENT_TYPE
