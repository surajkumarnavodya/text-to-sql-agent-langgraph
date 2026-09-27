"""Resolves the real client IP for one request, with an explicit, opt-in,
exact-hop-count trusted-proxy allowance.

Enterprise scalability/security assessment (2026-09-27): every existing
rate-limit/audit-log call site (`api/rate_limit.py`, `api/main.py`'s
`_rate_limit_key` IP fallback, `api/identity_auth.py`'s pre-auth endpoints)
reads `request.client.host` directly and never consults
`X-Forwarded-For`/`X-Real-IP` -- correct today
(`tests/security/test_rate_limit_header_spoofing.py` locks this in as a
deliberate choice, not an oversight, for a directly-exposed, single-instance
deployment with no trusted proxy in front of it at all).

That default stops being correct the moment a real reverse proxy or load
balancer sits in front of this app (the horizontally-scaled target
architecture this module was added for): `request.client.host` then becomes
the proxy's own address for *every* caller, collapsing every distinct client
into one shared rate-limit bucket and one shared audit-log "who did this" IP
-- a real availability problem (innocent users rate-limited together because
one of them tripped a limit) and an accountability gap, not merely an
inaccuracy.

This module is the one, shared, opt-in fix for both -- `Settings
.trusted_proxy_count` (default 0) governs it, and every existing call site
above should route through `resolve_client_ip` instead of reading
`request.client.host` directly, so enabling trusted-proxy support is a
single `.env` change, not a multi-file hunt.
"""

from __future__ import annotations

from starlette.requests import Request

from config.settings import Settings


def resolve_client_ip(request: Request, settings: Settings) -> str:
    """Returns the best-known client IP for `request`.

    Args:
        request: The current request.
        settings: Application settings (`trusted_proxy_count`).

    Returns:
        With `trusted_proxy_count == 0` (the default): exactly
        `request.client.host` (or `"unknown"` if the ASGI server didn't
        supply one) -- byte-for-byte what every existing call site already
        used, so this is a pure refactor with zero behavior change unless an
        operator explicitly opts in.

        With `trusted_proxy_count == N > 0`: the address the Nth trusted
        proxy itself observed as its own connecting peer, read as the Nth
        entry from the *right* of a comma-separated `X-Forwarded-For`
        header -- never the leftmost entry, which is exactly the part of
        the header a client can freely pre-seed with fake values before it
        ever reaches the first proxy (the well-documented XFF-spoofing
        pitfall of naively trusting "the first entry"). This is the same
        "trust an exact hop count, count from the trusted end" algorithm
        Werkzeug's `ProxyFix` and Django's `django-referrer-policy`-adjacent
        proxy guidance both use, applied here without adding either as a
        dependency.

        Falls back to `request.client.host` (never to an untrusted header
        value) whenever the header is missing, empty, or has *fewer*
        comma-separated entries than `trusted_proxy_count` -- a
        misconfigured or incomplete proxy chain fails closed to the literal
        TCP peer rather than trusting client-supplied data it can't verify
        the provenance of.
    """
    direct_peer = request.client.host if request.client else "unknown"
    if settings.trusted_proxy_count <= 0:
        return direct_peer

    forwarded_for = request.headers.get("x-forwarded-for")
    if not forwarded_for:
        return direct_peer

    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    if len(hops) < settings.trusted_proxy_count:
        return direct_peer

    return hops[-settings.trusted_proxy_count]
