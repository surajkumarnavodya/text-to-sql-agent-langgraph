"""FastAPI router for direct media search -- independent of the
conversational `/ask` flow, per this feature's own requirement. Returns
raw ranked hits (a templated "Found N result(s)" summary, not an
LLM-composed answer) -- a caller that wants a natural-language answer
citing what was found should ask through `/ask` instead, which routes
through `agent.orchestrator.nodes.media_search_node`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from api.auth import verify_api_key
from api.rate_limit import enforce_api_action_rate_limit
from api.schemas import MediaSearchHitOut, MediaSearchRequest, MediaSearchResultOut
from config.settings import get_settings
from media.search import search_media

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.post("/search/media", response_model=MediaSearchResultOut)
def search_media_endpoint(payload: MediaSearchRequest, request: Request) -> MediaSearchResultOut:
    """Searches the media library directly for `payload.query`, restricted
    to `payload.media_type` if given. 404 when `Settings.enable_media_search`
    is off, same "an infra flag that's off means the route doesn't exist"
    posture `api/voice.py`/`api/media.py` already use."""
    settings = get_settings()
    if not settings.enable_media_search:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media search is not enabled."
        )
    enforce_api_action_rate_limit(request, "media_search", settings)

    hits = search_media(payload.query, settings, media_type=payload.media_type)
    return MediaSearchResultOut(
        answer=f"Found {len(hits)} result(s)." if hits else "No matching media found.",
        status="succeeded" if hits else "insufficient_information",
        hits=[
            MediaSearchHitOut(
                media_id=hit.media_id,
                media_type=hit.media_type,
                caption=hit.caption,
                timestamp_start=hit.timestamp_start,
                timestamp_end=hit.timestamp_end,
            )
            for hit in hits
        ],
    )
