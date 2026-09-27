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

from agent.rate_limit import BoundedLimiterCache
from config.settings import Settings
from security.client_ip import resolve_client_ip
from security.oidc import AuthIdentity, real_caller_subject

RATE_LIMIT_MESSAGE = "Too many requests -- please wait a moment and try again."

# Bounded (see BoundedLimiterCache's docstring) -- previously a bare dict
# that grew one entry per distinct `f"{action}:{client_ip}"` key forever
# (2026 Phase 1 security review, finding API-02).
_limiters = BoundedLimiterCache()


def enforce_api_action_rate_limit(
    request: Request,
    action: str,
    settings: Settings,
    identity: AuthIdentity | None = None,
) -> None:
    """Raises `HTTPException(429)` if `action` has been called too many
    times from this caller in the last minute
    (`Settings.api_action_rate_limit_per_minute`). Call at the top of a
    route handler that needs this -- before any real work happens.

    Args:
        request: The current request.
        action: A short, stable name for the action being limited (e.g.
            "execute", "document_upload") -- each gets its own independent
            budget, keyed per-caller.
        settings: Application settings.
        identity: The caller's `AuthIdentity`, when the route handler
            already resolved one (every call site except `api/voice.py`'s
            two routes, which apply their permission check at the router
            level rather than binding it to a function parameter -- see
            that module's own `APIRouter(dependencies=[...])` pattern).
            When provided, keys on `security.oidc.real_caller_subject`
            instead of client IP -- enterprise scalability assessment
            (2026-09-27): mirrors `api/main.py`'s own `/ask`-scoped
            `_rate_limit_key`, closing a gap where this limiter
            rate-limited purely by IP even for an authenticated caller
            (meaningless behind a load balancer/carrier NAT, and coarser
            than necessary even on a single instance). `None` (the
            default) preserves the original IP-only behavior exactly.

    The IP-fallback path (used when `identity` is `None`, or when
    `real_caller_subject` reports no real per-caller identity exists for
    the current auth mode) goes through `security.client_ip
    .resolve_client_ip` rather than raw `request.client.host` -- with
    `Settings.trusted_proxy_count` at its default of 0 this is
    byte-for-byte the same value as before, so this is a pure refactor for
    every existing deployment; it only starts reading `X-Forwarded-For` for
    an operator who explicitly configures a trusted reverse proxy count.
    """
    subject = real_caller_subject(identity) if identity is not None else None
    caller = (
        f"user:{subject}" if subject is not None else f"ip:{resolve_client_ip(request, settings)}"
    )
    key = f"{action}:{caller}"
    limiter = _limiters.get_or_create(
        key,
        max_events=settings.api_action_rate_limit_per_minute,
        window_seconds=60.0,
        name=f"api_action[{key}]",
    )

    result = limiter.check()
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=RATE_LIMIT_MESSAGE,
            headers={"Retry-After": str(int(result.retry_after_seconds) + 1)},
        )
