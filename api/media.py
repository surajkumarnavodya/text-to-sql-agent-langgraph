"""FastAPI router serving generated media (image/video) bytes.

The API equivalent of `api/documents.py`'s PDF download route -- a
generated asset's bytes are downloaded once (`media_gen.download
.download_media_bytes`) and cached in-process under an opaque id
(`media_gen.cache.get_media_cache`), never re-fetched from the provider's
own CDN URL. This route is the only way a client (the React frontend) ever
gets those bytes -- see `agent.orchestrator.state.MediaGenerationResult
.media_id`'s docstring for why the raw provider URL is never exposed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from agent.authz import Permission
from api.authz import require_permission
from media_gen.cache import get_media_cache

router = APIRouter(dependencies=[Depends(require_permission(Permission.MEDIA_GENERATE))])


@router.get("/media/{media_id}")
def get_media(media_id: str) -> Response:
    """Streams one generated asset's bytes -- 404 if `media_id` was never
    stored, already evicted (the cache is bounded, see `media_gen/cache.py`),
    or the process has since restarted (the cache is process-lifetime only,
    not persisted)."""
    entry = get_media_cache().get(media_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media not found or expired."
        )
    # X-Content-Type-Options: nosniff -- defense-in-depth alongside
    # media_gen.download's own Content-Type allowlist (2026 Phase 3 file-
    # upload security review, finding G1): even an allowed value
    # (image/video/audio) should never be MIME-sniffed by the browser into
    # something more permissive than declared.
    return Response(
        content=entry.data,
        media_type=entry.content_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )
