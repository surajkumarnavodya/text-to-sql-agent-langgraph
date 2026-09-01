"""In-memory sliding-window rate limiting.

A basic safeguard appropriate for a local, single-user-oriented tool -- not
a substitute for real rate limiting in a multi-tenant deployment (see
SECURITY.md). Deliberately simple: no persistence, no distributed
coordination, resets on every app restart. Two independent limiters are
built from the same `SlidingWindowRateLimiter` class, at different scopes:

  - **Question submissions** (`Settings.question_rate_limit_per_minute`,
    default 10/min): genuinely per Streamlit session -- the UI
    (`ui/app.py`) owns an instance in `st.session_state` and checks it
    before ever calling `agent.graph.run_agent`. Protects against a human
    (or a script) hammering the chat box faster than the pipeline can
    reasonably keep up.
  - **LLM generation calls** (`Settings.llm_call_rate_limit_per_minute`,
    default 20/min, deliberately *stricter*): process-global, checked
    inside `agent.nodes.generate_sql_node` before every actual call to
    Ollama -- including retries. This is what actually bounds the retry
    loop: a single question can burn up to `MAX_RETRIES + 1` LLM calls on
    its own, so the question-level limit alone doesn't cap total LLM load.
    Process-global rather than per-session is a deliberate simplification:
    the retry loop lives inside a single `run_agent()` graph execution,
    which is rebuilt fresh every call, so there's no natural per-session
    object to thread a stateful limiter through without passing it as a
    live object inside `AgentState` (awkward next to the otherwise-plain-
    data state model). For this app's actual target -- one local user --
    process-global and per-session are practically equivalent; a real
    multi-session deployment would need to revisit this.

Every trip logs through this module's own logger (`agent.rate_limit` --
distinct from `agent.nodes`'/`agent.input_guard`'s categories, so rate-limit
events are easy to find/filter/alert on separately from validator
rejections or retry-loop errors).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# User-facing text -- calm and non-technical, consistent with the fallback-
# messaging pattern from the adversarial-input hardening work (agent.
# input_guard._MESSAGES): no stack trace, no raw counter values, just what
# happened and what to do.
QUESTION_LIMIT_MESSAGE = (
    "You're asking questions faster than I can process them -- please wait a moment."
)
LLM_CALL_LIMIT_MESSAGE = (
    "The system is handling a lot of requests right now -- please wait a moment and try again."
)


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of one `SlidingWindowRateLimiter.check()` call.

    Attributes:
        allowed: Whether this event may proceed. A denied check does NOT
            get recorded in the window -- a flood of denied attempts must
            not itself extend how long the caller stays blocked.
        retry_after_seconds: Best-effort estimate of how long until the
            oldest recorded event ages out of the window and a slot frees
            up. 0.0 when allowed.
    """

    allowed: bool
    retry_after_seconds: float = 0.0


class SlidingWindowRateLimiter:
    """Caps events to `max_events` within any trailing `window_seconds`.

    Classic sliding-window-log approach: a deque of timestamps for
    previously *allowed* events. On each check, timestamps older than the
    window are pruned first, then the event is allowed (and recorded) only
    if fewer than `max_events` remain. Intentionally the simplest correct
    approach for this scale -- no token buckets, no external store, safe to
    share across threads only in the loose sense the GIL provides (fine for
    this app's actual concurrency profile: a handful of Streamlit script
    reruns, not a real multi-threaded server).
    """

    def __init__(self, max_events: int, window_seconds: float, name: str) -> None:
        """
        Args:
            max_events: Maximum allowed events within the window.
            window_seconds: Width of the trailing window, in seconds.
            name: Short identifier for this limiter, used only in log lines
                (e.g. "question_submissions", "llm_generation_calls") so a
                trip is attributable at a glance.
        """
        self._max_events = max_events
        self._window_seconds = window_seconds
        self._name = name
        self._events: deque[float] = deque()

    def check(self, now: float | None = None) -> RateLimitResult:
        """Records and allows this event, or denies it if the window is full.

        Args:
            now: Override for the current time (`time.monotonic()` units),
                for deterministic tests. Defaults to the real clock.

        Returns:
            A `RateLimitResult`.
        """
        current = time.monotonic() if now is None else now
        cutoff = current - self._window_seconds
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

        if len(self._events) >= self._max_events:
            retry_after = max(0.0, self._events[0] + self._window_seconds - current)
            logger.warning(
                "[rate_limit] %s limiter tripped: %d/%d events in the last %.0fs "
                "(retry after %.1fs)",
                self._name,
                len(self._events),
                self._max_events,
                self._window_seconds,
                retry_after,
            )
            return RateLimitResult(allowed=False, retry_after_seconds=retry_after)

        self._events.append(current)
        return RateLimitResult(allowed=True)

    def reset(self) -> None:
        """Clears all recorded events. Mainly for tests and session resets."""
        self._events.clear()


_llm_call_limiter: SlidingWindowRateLimiter | None = None


def get_llm_call_limiter(max_calls_per_minute: int) -> SlidingWindowRateLimiter:
    """Returns the process-wide LLM-call limiter, creating it on first use.

    Args:
        max_calls_per_minute: `Settings.llm_call_rate_limit_per_minute`.
            Only used to construct the limiter the *first* time this is
            called in the process -- like `config.settings.get_settings`'s
            own `lru_cache`, a config change requires a process restart to
            take effect, which is consistent with how every other
            process-lifetime singleton in this codebase behaves (see
            `db.connection._cached_engine`).
    """
    global _llm_call_limiter
    if _llm_call_limiter is None:
        _llm_call_limiter = SlidingWindowRateLimiter(
            max_events=max_calls_per_minute, window_seconds=60.0, name="llm_generation_calls"
        )
    return _llm_call_limiter
