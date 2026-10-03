"""Structured security-event logging: one consistent event shape, one
dedicated logger.

This app already detects and logs a wide range of security-relevant events
-- input rejections (`agent/input_guard.py`), validator safety violations
(`agent/sql_validator.py`, via `agent/nodes.py`), rate-limit trips
(`agent/rate_limit.py`), schema anomalies
(`agent.sql_validator.find_unexpected_table_references`) -- but each does so
in its own module's own prose log format. That's fine for reading the
terminal live, but makes it harder to build alerting or SIEM ingestion on
top of, since there's no single, consistently-shaped event stream to point
a log pipeline at.

This module is **additive, not a replacement**: every existing
`logger.warning(...)` call at those sites stays exactly as it is. This gives
those same call sites one more line, in one consistent shape, on a
dedicated `security.audit` logger -- a distinct category from any
individual module's own logger, so a log pipeline can filter/route/alert on
security events alone, the same way `agent.rate_limit`'s own distinct
logger category already lets rate-limit events be filtered separately from
ordinary retry-loop logs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextvars import ContextVar, Token
from typing import Literal, TypedDict

from security.sanitization import truncate_for_log

logger = logging.getLogger("security.audit")

# Holds the current request's correlation ID, if any -- set once per request
# by `api/middleware.py` and read automatically by `log_security_event`
# below, so every existing `log_security_event(...)` call site in
# `agent/nodes.py` becomes correlation-ID-aware with zero changes to that
# module. `contextvars` (rather than a thread-local) is required because
# Starlette dispatches a sync endpoint to a worker thread via
# `anyio.to_thread.run_sync`, which explicitly copies the calling context
# into that thread -- a plain `threading.local` would not see the value set
# by the middleware, which runs in the event loop's own async context.
# Outside a request (a CLI script, a test), this simply stays unset and
# `log_security_event` omits the field, exactly as before.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


# The current request's tenant, if resolved (Prompt 20). Same
# `contextvars` mechanism and the same reasoning as `_correlation_id` above:
# a security event is far more actionable when a reader can tell *whose*
# tenant it came from, and threading an explicit `tenant_id=` argument
# through all ~40 existing `log_security_event` call sites would have been a
# large, error-prone diff where any missed site silently produces an
# untagged event.
#
# Bound by `agent.graph.run_agent` and `agent.orchestrator.graph
# .run_orchestrated` (both already receive `tenant_id` explicitly, and both
# run on the same thread as every node that emits an event) and by
# `api/auth.py` for its own authentication-failure events. Deliberately not
# bound in the correlation-ID middleware: the tenant is not known yet at
# that point, and FastAPI runs a sync dependency in its own copied context,
# so a value set there would not be visible to the endpoint anyway.
#
# **Known limitation, inherited from `_correlation_id` rather than
# introduced here:** a `contextvars` value does not cross a raw
# `concurrent.futures.ThreadPoolExecutor` boundary (unlike anyio's
# thread dispatch, which copies the context). `api/main.py`'s `/ask` pool is
# such an executor -- which is exactly why this is bound *inside*
# `run_orchestrated`/`run_agent`, on the worker side of that boundary,
# rather than in the route handler.
_tenant_id: ContextVar[str | None] = ContextVar("audit_tenant_id", default=None)


def set_audit_tenant_id(value: str | None) -> Token[str | None]:
    """Binds `value` as the tenant stamped onto this context's audit events.

    Returns the `Token` needed to restore the previous value via
    `reset_audit_tenant_id` -- callers must reset in a `finally` block so a
    tenant never leaks into whatever reuses this thread/task next, exactly
    as `set_correlation_id` requires.
    """
    return _tenant_id.set(value)


def reset_audit_tenant_id(token: Token[str | None]) -> None:
    """Restores the audit tenant to what it was before `set_audit_tenant_id`."""
    _tenant_id.reset(token)


def get_audit_tenant_id() -> str | None:
    """Returns the tenant audit events are currently stamped with, or `None`."""
    return _tenant_id.get()


def set_correlation_id(value: str | None) -> Token[str | None]:
    """Binds `value` as the current context's correlation ID.

    Returns the `Token` needed to restore the previous value via
    `reset_correlation_id` -- callers (in practice, just `api/middleware.py`)
    must reset in a `finally` block so the ID doesn't leak into whatever
    reuses this thread/task next (e.g. a worker thread pool).
    """
    return _correlation_id.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    """Restores the correlation ID to what it was before `set_correlation_id`."""
    _correlation_id.reset(token)


def get_correlation_id() -> str | None:
    """Returns the current request's correlation ID, or None outside a request."""
    return _correlation_id.get()


class CorrelationIdLogFilter(logging.Filter):
    """Stamps `record.correlation_id` from the current request context onto
    every log record passing through the handler this filter is attached to.

    2026 Phase 3 observability gap: before this, a request's correlation ID
    was only visible on `security.audit` events (`log_security_event` reads
    the contextvar explicitly, above) -- every *ordinary* `logger.info`/
    `.warning`/`.error` call in `agent/nodes.py`, `rag/`, `db/`,
    `media_gen/`, `search/`, etc. used a plain per-module `logging.getLogger`
    with no correlation ID at all, making it impossible to grep one
    request's full trace across the API -> LangGraph -> RAG/SQL/external-call
    boundary the way `docs/OBSERVABILITY.md` describes. Attaching this filter
    to the root handler (`config.settings.configure_logging`) fixes that for
    every existing call site with zero changes to any of those modules --
    `logging.Filter` runs on every record before formatting, and the
    correlation ID is already available via the same `contextvars.ContextVar`
    `log_security_event` reads.

    Outside a request (a CLI script, a test, startup logging before the
    middleware has run) `get_correlation_id()` is `None`; this renders as
    `"-"` rather than the string `"None"`, so the format string's column
    stays a stable width and `grep -v ' correlation_id=- '` cleanly isolates
    request-scoped log lines from background ones.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


class TenantIdLogFilter(logging.Filter):
    """Stamps `record.tenant_id` from the current request context onto
    every log record passing through the handler this filter is attached
    to -- the identical mechanism `CorrelationIdLogFilter` above already
    established, applied to the second piece of per-request context this
    codebase tracks (Prompt 23, observability/evaluation/reliability).

    Before this, a request's tenant was only visible on `security.audit`
    events (`log_security_event` reads `_tenant_id` explicitly) -- every
    *ordinary* `logger.info`/`.warning`/`.error` call (`agent/nodes.py`'s
    `[timing]`/`[trace]` lines included) carried a correlation ID but no
    tenant, making "which tenant's request produced this log line" only
    answerable by also having the audit-event stream open at the same
    time. Attaching this filter to the root handler
    (`config.settings.configure_logging`) fixes that for every existing
    call site with zero changes to any of those modules, exactly like
    `CorrelationIdLogFilter` did for correlation IDs.

    Renders as `"-"` (never the string `"None"`) when unbound, same
    stable-width/greppability reasoning as `CorrelationIdLogFilter`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.tenant_id = get_audit_tenant_id() or "-"
        return True


Severity = Literal["info", "warning", "critical"]

_LEVEL_MAP: dict[Severity, int] = {
    "info": logging.INFO,
    "warning": logging.WARNING,
    "critical": logging.ERROR,
}

# Context values longer than this are truncated (via
# `security.sanitization.truncate_for_log`) before being rendered --
# context often carries attacker-controlled text (a rejected question, a
# flagged SQL fragment), and an unbounded value here would be exactly the
# log-flooding vector `truncate_for_log` already exists to prevent
# elsewhere in this codebase.
_MAX_CONTEXT_VALUE_LENGTH = 200


class SecurityEventRecord(TypedDict):
    """One entry in the in-process ring buffer below -- the shape
    `GET /platform-admin/security-events` (Prompt 28) returns."""

    timestamp: str
    event_type: str
    severity: Severity
    detail: str
    correlation_id: str | None
    tenant_id: str | None
    context: dict[str, str]


#: Prompt 28 (`28_GLOBAL_PLATFORM_ADMIN_DASHBOARD_CONTRACT.md`): a bounded,
#: thread-safe, in-process ring buffer of the most recent security events,
#: fed from inside `log_security_event` itself -- every one of this
#: codebase's ~40 existing call sites starts populating it with zero
#: changes to any of them, the same "instrument the one shared chokepoint,
#: not every call site" approach `security/audit_log.py`'s own
#: `CorrelationIdLogFilter`/`TenantIdLogFilter` already use for ordinary
#: log lines. Before this, a security event was visible only by reading
#: the live process log stream (or whatever external log pipeline an
#: operator has wired up) -- there was no queryable "what just happened"
#: view at all, unlike `observability.metrics.PerformanceMetrics`'s
#: equivalent rollup for ordinary request timings.
#:
#: **Deliberate limits, matching `PerformanceMetrics`'s own disclosed
#: posture for the identical reason**: single-process, in-memory only (a
#: multi-worker deployment has one independent buffer per worker), resets
#: on restart, bounded by count (`_MAX_RECENT_EVENTS`), not time. This is
#: an operational "what just happened" view, not a durable SIEM -- a real
#: deployment wanting long-term retention still needs a real log
#: pipeline, which this app does not attempt to replace.
_MAX_RECENT_EVENTS = 1000
_recent_events: deque[SecurityEventRecord] = deque(maxlen=_MAX_RECENT_EVENTS)
_recent_events_lock = threading.Lock()


def _record_recent_event(
    event_type: str, severity: Severity, detail: str, context: dict[str, object]
) -> None:
    """Appends one entry to the ring buffer. Never raises -- called from
    inside `log_security_event`'s own outer `try`, but kept defensive on
    its own too, since a ring-buffer write must never be why a security
    event fails to reach the log line that already carries it."""
    try:
        rendered_context = {
            key: truncate_for_log(repr(value), _MAX_CONTEXT_VALUE_LENGTH)
            for key, value in context.items()
        }
        with _recent_events_lock:
            _recent_events.append(
                {
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "event_type": event_type,
                    "severity": severity,
                    "detail": detail,
                    "correlation_id": _correlation_id.get(),
                    "tenant_id": _tenant_id.get(),
                    "context": rendered_context,
                }
            )
    except Exception:  # noqa: BLE001 - must never break the caller
        pass


def recent_security_events(
    *, limit: int = 100, severity: Severity | None = None, event_type: str | None = None
) -> list[SecurityEventRecord]:
    """Returns the most recent security events, newest first, optionally
    filtered -- `GET /platform-admin/security-events`'s own query.

    `limit` caps how many are returned, never how many are scanned (the
    buffer itself is already bounded to `_MAX_RECENT_EVENTS`, so a filtered
    query is always a cheap, fixed-size scan).
    """
    with _recent_events_lock:
        events = list(_recent_events)
    events.reverse()
    if severity is not None:
        events = [event for event in events if event["severity"] == severity]
    if event_type is not None:
        events = [event for event in events if event["event_type"] == event_type]
    return events[:limit]


def log_security_event(
    event_type: str,
    severity: Severity,
    detail: str,
    **context: object,
) -> None:
    """Emits one structured security event on the dedicated audit logger.

    Args:
        event_type: A short, stable identifier for the kind of event (e.g.
            "input_rejected", "sql_safety_violation", "rate_limit_tripped",
            "schema_anomaly", "sensitive_column_blocked",
            "possible_rag_poisoning"). Stable across calls so a log
            pipeline can filter/alert on it specifically.
        severity: "info" (an expected, routine denial -- e.g. an ordinary
            rate-limit trip), "warning" (worth a human noticing -- e.g. a
            validator safety violation or a sensitive-column block), or
            "critical" (worth paging on -- reserved for a future event
            class; nothing in this app emits it yet, but the level exists
            so one has somewhere to go without a new function).
        detail: Human-readable summary of what happened.
        **context: Additional structured fields (e.g. `reason=...`,
            `violation_type=...`, `table=...`). Values are rendered via
            `repr()` and capped via `truncate_for_log` -- callers do not
            need to pre-truncate attacker-controlled text themselves.

    Stamps `tenant_id=` automatically when one is bound for this context
    (Prompt 20, see `set_audit_tenant_id`) -- no call site passes it, and a
    caller that explicitly passes `tenant_id=` as context still works, it
    simply appears twice.

    Never raises: a logging bug must not be the reason an otherwise-normal
    request fails, the same fail-safe principle applied throughout this
    codebase's other non-critical-path logging.
    """
    try:
        rendered_context = " ".join(
            f"{key}={truncate_for_log(repr(value), _MAX_CONTEXT_VALUE_LENGTH)}"
            for key, value in context.items()
        )
        message = f"event={event_type} severity={severity} detail={detail!r}"
        correlation_id = _correlation_id.get()
        if correlation_id is not None:
            message = f"{message} correlation_id={correlation_id}"
        tenant_id = _tenant_id.get()
        if tenant_id is not None:
            message = f"{message} tenant_id={tenant_id}"
        if rendered_context:
            message = f"{message} {rendered_context}"
        logger.log(_LEVEL_MAP[severity], message)
        _record_recent_event(event_type, severity, detail, context)
    except Exception:  # noqa: BLE001 - logging must never break the caller
        logger.exception("[audit_log] failed to emit security event event_type=%r", event_type)
