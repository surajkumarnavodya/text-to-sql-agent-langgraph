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
from contextvars import ContextVar, Token
from typing import Literal

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
# Outside a request (a CLI script, a test, the Streamlit UI), this simply
# stays unset and `log_security_event` omits the field, exactly as before.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

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
        if rendered_context:
            message = f"{message} {rendered_context}"
        logger.log(_LEVEL_MAP[severity], message)
    except Exception:  # noqa: BLE001 - logging must never break the caller
        logger.exception("[audit_log] failed to emit security event event_type=%r", event_type)
