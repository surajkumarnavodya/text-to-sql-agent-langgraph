"""FastAPI router serving media-library bytes (ingested images and video
segment thumbnails) -- the persistent-library counterpart to
`api/media.py`'s ephemeral generated-media serving.

Deliberately a **separate** lookup path from `media_gen.cache.MediaCache`:
that cache is in-memory, process-lifetime, and bounded to 100 entries with
FIFO eviction -- built for short-lived *generated* media, completely the
wrong fit for a persistent library meant to stay searchable indefinitely.
This route instead looks up `media_id` directly in the Chroma collections
`media/store.py` manages, resolves the file path from stored metadata, and
re-validates that path is still inside the expected root directory before
ever opening it -- a local-path-traversal defense in the same spirit as
`media_gen/download.py`'s SSRF hardening, applied to disk paths instead of
URLs, in case a stored path were ever stale or manipulated.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from api.auth import verify_api_key
from config.settings import get_settings
from media.keyframes import THUMBNAIL_DIR
from media.store import get_image_metadata, get_segment_metadata

router = APIRouter(dependencies=[Depends(verify_api_key)])

_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}

_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Media not found or expired."
)


def _resolve_safe_path(raw_path: str, allowed_root: Path) -> Path:
    """Resolves a stored path and verifies it's still inside `allowed_root`
    before returning it -- never trust a Chroma-stored path blindly, even
    though it's this app's own data rather than direct user input."""
    resolved = Path(raw_path).resolve()
    if not resolved.is_relative_to(allowed_root.resolve()) or not resolved.is_file():
        raise _NOT_FOUND
    return resolved


@router.get("/media/library/{media_id}")
def get_library_media(media_id: str) -> FileResponse:
    """Streams one library asset's bytes: the original file for an image
    hit, or the representative keyframe thumbnail for a video segment hit
    -- a full clip is never streamed (see `media/keyframes.py`'s module
    docstring for why frame + timestamp is this feature's chosen
    tradeoff). 404 if `media_id` is unknown, media search is disabled, or
    the underlying file is missing/moved.
    """
    settings = get_settings()
    if not settings.enable_media_search or not settings.media_library_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media search is not enabled."
        )

    image_metadata = get_image_metadata(media_id, settings)
    if image_metadata is not None:
        path = _resolve_safe_path(str(image_metadata["source_path"]), settings.media_library_path)
        content_type = _IMAGE_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=content_type)

    segment_metadata = get_segment_metadata(media_id, settings)
    if segment_metadata is not None:
        # Thumbnails live under media/.thumbnails/, not the configured
        # media library root -- resolved against that directory instead.
        path = _resolve_safe_path(str(segment_metadata["thumbnail_path"]), THUMBNAIL_DIR)
        return FileResponse(path, media_type="image/jpeg")

    raise _NOT_FOUND
