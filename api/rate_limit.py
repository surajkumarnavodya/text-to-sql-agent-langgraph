"""Per-client-IP rate limiting for API routes that previously had none.

`api/main.py`'s `/ask` has always had its own per-IP question-submission
limiter (`_limiter_for`), and the SQL-generation retry loop has its own
process-wide LLM-call limiter (`agent.rate_limit`). Several other
state-changing/resource-intensive routes had no rate limit at all: `POST
/execute` (runs arbitrary validated SQL against the live database), `POST
/schema/refresh` (re-introspects + re-embeds every configured database),
the mutating `/documents` routes (PDF ingestion is expensive), and the new
`POST /generate/confirm` (spends real, metered IMA credits). This module
is the one shared limiter those routes call into, keyed per-action so
hammering one doesn't share budget with another -- mirrors `api/main.py`'s
own `_limiter_for` pattern, generalized.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from agent.rate_limit import SlidingWindowRateLimiter
from config.settings import Settings

RATE_LIMIT_MESSAGE = "Too many requests -- please wait a moment and try again."

_limiters: dict[str, SlidingWindowRateLimiter] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce_api_action_rate_limit(request: Request, action: str, settings: Settings) -> None:
    """Raises `HTTPException(429)` if `action` has been called too many
    times from this client IP in the last minute
    (`Settings.api_action_rate_limit_per_minute`). Call at the top of a
    route handler that needs this -- before any real work happens."""
    key = f"{action}:{_client_ip(request)}"
    limiter = _limiters.get(key)
    if limiter is None:
        limiter = SlidingWindowRateLimiter(
            max_events=settings.api_action_rate_limit_per_minute,
            window_seconds=60.0,
            name=f"api_action[{key}]",
        )
        _limiters[key] = limiter

    result = limiter.check()
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=RATE_LIMIT_MESSAGE,
            headers={"Retry-After": str(int(result.retry_after_seconds) + 1)},
        )
