"""FastAPI router for the media-generation human-approval confirmation step.

`agent.orchestrator.nodes.generation_node` proposes a generation
(`status="pending_approval"`) but -- when `Settings.require_generation_approval`
is on, the default -- never calls the real, metered IMA Studio API itself.
`POST /generate/confirm` is the only route that does: it calls
`agent.orchestrator.nodes.execute_generation` directly, the exact function
`generation_node` would call itself if approval were disabled. This mirrors
`POST /execute`'s relationship to `/ask` for the SQL pipeline's own
"Confirm and Run" gate (see that endpoint's docstring in `api/main.py`)
applied to a different, real-money-spending source.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from agent.orchestrator.nodes import execute_generation, infer_media_kind
from api.auth import verify_api_key
from api.rate_limit import enforce_api_action_rate_limit
from api.schemas import GenerateConfirmRequest, MediaGenerationResultOut
from config.settings import get_settings

router = APIRouter(dependencies=[Depends(verify_api_key)])


@router.post("/generate/confirm", response_model=MediaGenerationResultOut)
def confirm_generation(
    payload: GenerateConfirmRequest, request: Request
) -> MediaGenerationResultOut:
    """The human-approval confirmation step for media generation.

    Calls `execute_generation` directly, which re-runs its own safety and
    rate-limit checks regardless of whatever `generation_node` already
    showed the caller in a prior `/ask` response -- this route is a
    separate entry point and must not skip them just because a client
    claims it already saw a proposal. The media kind is always re-inferred
    from `payload.question` server-side (`infer_media_kind`), never
    accepted from the client -- see `GenerateConfirmRequest`'s docstring.
    Rate-limited per client IP (`enforce_api_action_rate_limit`) on top of
    `execute_generation`'s own process-wide media-generation limiter --
    this one bounds how often *this caller* can even attempt a confirm.
    """
    settings = get_settings()
    enforce_api_action_rate_limit(request, "generate_confirm", settings)
    kind = infer_media_kind(payload.question)
    result = execute_generation(payload.question, kind, settings)
    return MediaGenerationResultOut(
        answer=result["answer"],
        status=result["status"],
        media_id=result.get("media_id"),
        media_type=result.get("media_type"),
        model=result.get("model"),
    )
