"""In-memory sliding-window rate limiting, plus in-flight concurrency
limiting (`ConcurrencyLimiter`/`BoundedConcurrencyLimiterCache`, added
alongside the scale-out hardening pass -- see
`docs/SCALE_OUT_PROMPT.md`).

A real safeguard, but still process-local -- not a substitute for
distributed rate limiting across multiple replicas (see SECURITY.md and
`docs/SCALE_OUT_PROMPT.md`'s Phase 3, which moves this to Redis). No
persistence, no cross-process coordination, resets on every app restart.
Several independent limiters are built from the same
`SlidingWindowRateLimiter`/`ConcurrencyLimiter` classes, at different
scopes:

  - **Question submissions** (`Settings.question_rate_limit_per_minute`,
    default 10/min): keyed by authenticated subject when one exists
    (`local`/`oidc` auth modes), else client IP (`none`/`static_token`
    modes, where every caller is otherwise indistinguishable) -- see
    `api/main.py`'s `_rate_limit_key`. `api/main.py` owns one instance per
    caller and checks it before ever calling
    `agent.orchestrator.graph.run_orchestrated`. Protects against a human
    (or a script) hammering the chat box faster than the pipeline can
    reasonably keep up.
  - **`/ask` in-flight concurrency** (`ConcurrencyLimiter`,
    `Settings.max_concurrent_ask_requests` process-global +
    `max_concurrent_ask_requests_per_caller` per the same caller key as
    above): a *rate* limit alone doesn't stop one caller from having
    several slow requests running at once, each holding a thread-pool
    slot, a DB connection, and an in-flight LLM call -- this bounds
    *concurrent* load directly. Checked before any work starts (a full
    limiter is an immediate 429, not a queued wait); released only when
    the underlying graph execution actually finishes, not when
    `api/main.py`'s outer request-timeout gives up waiting on it, so an
    abandoned-but-still-running call still correctly occupies its slot.
  - **LLM generation calls** (`Settings.llm_call_rate_limit_per_minute`,
    default 20/min, deliberately *stricter*): process-global, checked
    inside `agent.nodes.generate_sql_node` before every actual call to
    Ollama -- including retries. This is what actually bounds the retry
    loop: a single question can burn up to `MAX_RETRIES + 1` LLM calls on
    its own, so the question-level limit alone doesn't cap total LLM load.
    Process-global rather than per-caller is a deliberate simplification:
    the retry loop lives inside a single `run_agent()` graph execution,
    which is rebuilt fresh every call, so there's no natural per-caller
    object to thread a stateful limiter through without passing it as a
    live object inside `AgentState` (awkward next to the otherwise-plain-
    data state model) -- a real multi-replica deployment needs to revisit
    this regardless (see Phase 3 above), so a per-caller refinement here
    specifically wasn't prioritized ahead of that.

Every trip logs through this module's own logger (`agent.rate_limit` --
distinct from `agent.nodes`'/`agent.input_guard`'s categories, so rate-limit
events are easy to find/filter/alert on separately from validator
rejections or retry-loop errors).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
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
MEDIA_GENERATION_LIMIT_MESSAGE = (
    "Too many media generation requests right now -- please wait a moment and try again."
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
    this app's actual concurrency profile: a single-instance FastAPI
    process with a handful of concurrent requests, not a real distributed
    multi-tenant server).
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


class BoundedLimiterCache:
    """A dict of `SlidingWindowRateLimiter`, keyed by an arbitrary string
    and bounded to `max_entries` via least-recently-used eviction.

    Every per-key rate limiter in this codebase (`api/main.py`'s per-IP
    question-submission limiter, `api/rate_limit.py`'s per-IP-per-action
    limiter, and this module's own per-session expensive-source limiter --
    see `get_session_expensive_source_limiter`) used to be a bare,
    never-pruned `dict`. Each is keyed by a value at least partly outside
    this app's control -- a client IP, or, for the session-scoped one, a
    value the *client itself* supplies in the request body with nothing
    authenticating it (`get_session_expensive_source_limiter`'s own
    docstring already discloses that a caller can mint a fresh session_id
    per request to reset their cost ceiling). Nothing stopped that same
    trick from growing the backing dict without bound -- a real, previously
    unpatched memory-exhaustion vector requiring no more than an ordinary
    ability to send requests with a different key each time (2026 Phase 1
    security review, finding API-02).

    LRU eviction (rather than a background TTL-sweep task/thread) keeps the
    common case -- a small, stable set of real callers -- completely
    unaffected: an entry is only ever evicted once `max_entries` distinct
    keys are live at once, and the one evicted is always the
    least-recently-touched, i.e. the one least likely to belong to an
    active caller.
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        self._max_entries = max_entries
        self._cache: OrderedDict[str, SlidingWindowRateLimiter] = OrderedDict()

    def get_or_create(
        self, key: str, max_events: int, window_seconds: float, name: str
    ) -> SlidingWindowRateLimiter:
        """Returns the limiter for `key`, creating it on first use.

        `max_events`/`window_seconds`/`name` are only used the first time a
        given `key` is seen -- same "first call wins" convention as this
        module's other `get_*_limiter` singleton factories.
        """
        limiter = self._cache.get(key)
        if limiter is not None:
            self._cache.move_to_end(key)
            return limiter

        limiter = SlidingWindowRateLimiter(
            max_events=max_events, window_seconds=window_seconds, name=name
        )
        self._cache[key] = limiter
        if len(self._cache) > self._max_entries:
            evicted_key, _ = self._cache.popitem(last=False)
            logger.info(
                "[rate_limit] bounded cache at capacity (%d entries) -- evicted "
                "least-recently-used key %r to admit %r",
                self._max_entries,
                evicted_key,
                key,
            )
        return limiter

    def clear(self) -> None:
        """Drops every entry. Mainly for tests and session resets."""
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


ASK_CONCURRENCY_LIMIT_MESSAGE = (
    "The system is at capacity right now -- please wait a moment and try again."
)
PER_CALLER_ASK_CONCURRENCY_LIMIT_MESSAGE = (
    "You already have a question being processed -- please wait for it to finish before "
    "asking another."
)


class ConcurrencyLimiter:
    """Bounds how many events may be *concurrently in flight* at once --
    complementary to, not a replacement for, `SlidingWindowRateLimiter`'s
    per-time-window cap. A caller comfortably under a 10/minute rate limit
    can still have several of those ten requests still running
    simultaneously (the UI has no "wait for the last answer" lock, and a
    scripted caller has even less reason to wait) -- each one holds a
    thread-pool slot, a DB connection, and an in-flight LLM call for its
    full duration, so *concurrent* load, not just *rate*, is what actually
    exhausts shared worker capacity under a burst.

    `try_acquire()`/`release()` deliberately never block -- a full limiter
    means "reject this admission attempt now" (`api/main.py`'s `/ask`
    turns a failed `try_acquire()` into an immediate 429, before any LLM/DB
    work starts), not "queue and wait," which is what a real work queue
    (out of scope for this pass -- see `docs/SCALE_OUT_PROMPT.md` Phase 4)
    would be for.
    """

    def __init__(self, max_concurrent: int, name: str) -> None:
        self._max_concurrent = max_concurrent
        self._name = name
        self._lock = threading.Lock()
        self._in_flight = 0

    def try_acquire(self) -> bool:
        with self._lock:
            if self._in_flight >= self._max_concurrent:
                logger.warning(
                    "[rate_limit] %s concurrency limiter tripped: %d/%d in flight",
                    self._name,
                    self._in_flight,
                    self._max_concurrent,
                )
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        """Idempotent-safe against being called more times than
        `try_acquire()` succeeded (clamped at 0) -- a caller that's already
        handling its own "did I actually acquire this" bookkeeping (see
        `api/main.py`'s `_release_ask_slots`) still gets a harmless no-op
        rather than an assertion if something calls this defensively."""
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    def __len__(self) -> int:
        with self._lock:
            return self._in_flight


class BoundedConcurrencyLimiterCache:
    """A dict of `ConcurrencyLimiter`, keyed by an arbitrary string and
    bounded to `max_entries` via least-recently-used eviction -- the
    concurrency-limiter analog of `BoundedLimiterCache` above (same
    unbounded-key-growth concern: a caller key here is an authenticated
    subject when one exists, but still client IP otherwise, exactly like
    `BoundedLimiterCache`'s own keys -- see that class's docstring for the
    finding this pattern already closed once)."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._max_entries = max_entries
        self._cache: OrderedDict[str, ConcurrencyLimiter] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_create(self, key: str, max_concurrent: int, name: str) -> ConcurrencyLimiter:
        with self._lock:
            limiter = self._cache.get(key)
            if limiter is not None:
                self._cache.move_to_end(key)
                return limiter

            limiter = ConcurrencyLimiter(max_concurrent=max_concurrent, name=name)
            self._cache[key] = limiter
            if len(self._cache) > self._max_entries:
                evicted_key, _ = self._cache.popitem(last=False)
                logger.info(
                    "[rate_limit] bounded concurrency-limiter cache at capacity (%d entries) "
                    "-- evicted least-recently-used key %r to admit %r",
                    self._max_entries,
                    evicted_key,
                    key,
                )
            return limiter

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


_llm_call_limiter: SlidingWindowRateLimiter | None = None
_media_generation_limiter: SlidingWindowRateLimiter | None = None


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


_ask_concurrency_limiter: ConcurrencyLimiter | None = None
_per_caller_ask_concurrency_limiters = BoundedConcurrencyLimiterCache()


def get_ask_concurrency_limiter(max_concurrent: int) -> ConcurrencyLimiter:
    """Returns the process-wide `/ask` in-flight-request limiter, creating
    it on first use -- same first-call-wins singleton pattern as
    `get_llm_call_limiter`. This is the *global* admission gate
    `api/main.py`'s `ask()` checks before doing any work; see
    `get_per_caller_ask_concurrency_limiter` for the complementary
    per-caller one."""
    global _ask_concurrency_limiter
    if _ask_concurrency_limiter is None:
        _ask_concurrency_limiter = ConcurrencyLimiter(
            max_concurrent=max_concurrent, name="ask_requests_global"
        )
    return _ask_concurrency_limiter


def get_per_caller_ask_concurrency_limiter(
    caller_key: str, max_concurrent: int
) -> ConcurrencyLimiter:
    """Returns the in-flight `/ask` limiter for one caller (an authenticated
    subject when available, else client IP -- see `api/main.py`'s
    `_rate_limit_key`), creating it on first use for that key. Deliberately
    a *small* default (`Settings.max_concurrent_ask_requests_per_caller`,
    e.g. 2) -- this bounds one chatty caller (or a buggy client retrying
    without waiting) from occupying a large share of the global limiter's
    shared budget above."""
    return _per_caller_ask_concurrency_limiters.get_or_create(
        caller_key,
        max_concurrent=max_concurrent,
        name=f"ask_requests_per_caller[{caller_key}]",
    )


def get_media_generation_limiter(
    max_calls_per_window: int, window_seconds: float
) -> SlidingWindowRateLimiter:
    """Returns the process-wide media-generation-call limiter, creating it
    on first use -- same singleton pattern as `get_llm_call_limiter`, and
    deliberately its own, separate budget: image/video/audio generation
    costs meaningfully more per call than a text LLM turn (per-call cost,
    latency, and, for video, wall-clock time all much higher), so it must
    not share `llm_call_rate_limit_per_minute`'s budget unmodified. Checked
    inside `agent.orchestrator.nodes.generation_node` before every call
    into `media_gen`, process-global rather than per-session for the same
    reason `get_llm_call_limiter` is (see this module's own docstring)."""
    global _media_generation_limiter
    if _media_generation_limiter is None:
        _media_generation_limiter = SlidingWindowRateLimiter(
            max_events=max_calls_per_window,
            window_seconds=window_seconds,
            name="media_generation_calls",
        )
    return _media_generation_limiter


# Keyed by session_id -- unlike the two process-wide limiters above, this
# one deliberately needs per-session scope (see get_session_expensive_source_limiter's
# docstring for why). Bounded (see BoundedLimiterCache's docstring) since
# session_id is client-supplied and unauthenticated.
_session_expensive_source_limiters = BoundedLimiterCache()


def get_session_expensive_source_limiter(
    session_id: str, max_calls_per_window: int, window_seconds: float
) -> SlidingWindowRateLimiter:
    """Returns the per-session limiter on "expensive" orchestrator sources
    (generation, web -- see `agent.orchestrator.nodes._EXPENSIVE_SOURCES`),
    creating it on first use for that session.

    Every other limiter in this module is process-wide, which is
    deliberate for a single-user-oriented app (see this module's own
    docstring) -- but a single question can already fan out to *multiple*
    expensive sources at once (the router picking `["generation", "web"]`
    together is observed, real behavior, not a hypothetical), and nothing
    previously capped how many times *one session* could keep doing that
    across many questions. The per-source limiters (`get_media_generation_limiter`,
    the implicit one-call-per-Tavily-request in `search.web_search`) each
    bound their own call rate individually, but not the combination, and
    not per caller -- this closes that gap without needing a full
    session/identity system: `session_id` is already a real, if untrusted,
    per-conversation correlation token (`AskRequest.session_id`), good
    enough to scope a soft cost ceiling even though it isn't a substitute
    for real per-user authentication (see SECURITY.md's existing
    no-auth/no-tenant-isolation disclosure -- a caller can always mint a
    fresh session_id, exactly like the existing per-IP API rate limiters
    can be evaded by changing IP; this is a cost-control speed bump, not an
    access-control boundary).
    """
    return _session_expensive_source_limiters.get_or_create(
        session_id,
        max_events=max_calls_per_window,
        window_seconds=window_seconds,
        name=f"session_expensive_source[{session_id}]",
    )
