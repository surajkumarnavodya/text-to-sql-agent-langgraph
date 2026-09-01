"""Unit tests for the in-memory sliding-window rate limiter (agent/rate_limit.py).

All tests drive the clock explicitly via `check(now=...)` rather than
patching `time.monotonic` or sleeping -- deterministic and fast, and
exercises the exact same code path the real clock would.
"""

from __future__ import annotations

from agent.rate_limit import (
    LLM_CALL_LIMIT_MESSAGE,
    QUESTION_LIMIT_MESSAGE,
    SlidingWindowRateLimiter,
    get_llm_call_limiter,
)


class TestSlidingWindowRateLimiter:
    def test_allows_events_up_to_the_limit(self):
        limiter = SlidingWindowRateLimiter(max_events=3, window_seconds=60.0, name="test")
        for i in range(3):
            result = limiter.check(now=float(i))
            assert result.allowed is True

    def test_denies_the_event_past_the_limit(self):
        limiter = SlidingWindowRateLimiter(max_events=3, window_seconds=60.0, name="test")
        for i in range(3):
            limiter.check(now=float(i))

        result = limiter.check(now=3.0)

        assert result.allowed is False
        assert result.retry_after_seconds > 0

    def test_denied_events_are_not_recorded(self):
        """A flood of denied attempts must not itself extend the block --
        only successfully *allowed* events count toward the window."""
        limiter = SlidingWindowRateLimiter(max_events=1, window_seconds=60.0, name="test")
        limiter.check(now=0.0)  # consumes the only slot

        for i in range(1, 10):
            denied = limiter.check(now=float(i))
            assert denied.allowed is False

        # The window is still anchored to the single real event at t=0, not
        # pushed later by the 9 denied attempts -- it opens back up at t=60.
        result = limiter.check(now=60.0)
        assert result.allowed is True

    def test_sliding_window_frees_a_slot_once_old_events_age_out(self):
        limiter = SlidingWindowRateLimiter(max_events=2, window_seconds=60.0, name="test")
        limiter.check(now=0.0)
        limiter.check(now=10.0)
        assert limiter.check(now=20.0).allowed is False  # both slots still in window

        # The t=0 event is now outside the 60s window measured from t=61.
        result = limiter.check(now=61.0)
        assert result.allowed is True

    def test_retry_after_seconds_reflects_when_the_oldest_event_expires(self):
        limiter = SlidingWindowRateLimiter(max_events=1, window_seconds=60.0, name="test")
        limiter.check(now=0.0)

        result = limiter.check(now=45.0)

        assert result.allowed is False
        assert result.retry_after_seconds == 15.0  # 0 + 60 - 45

    def test_reset_clears_recorded_events(self):
        limiter = SlidingWindowRateLimiter(max_events=1, window_seconds=60.0, name="test")
        limiter.check(now=0.0)
        assert limiter.check(now=1.0).allowed is False

        limiter.reset()

        assert limiter.check(now=1.0).allowed is True

    def test_independent_limiters_do_not_share_state(self):
        a = SlidingWindowRateLimiter(max_events=1, window_seconds=60.0, name="a")
        b = SlidingWindowRateLimiter(max_events=1, window_seconds=60.0, name="b")

        a.check(now=0.0)

        assert b.check(now=0.0).allowed is True

    def test_uses_real_clock_when_now_is_not_provided(self):
        """Sanity check that the default path (no explicit `now`) also works,
        exercising the real time.monotonic() branch at least once."""
        limiter = SlidingWindowRateLimiter(max_events=1, window_seconds=60.0, name="test")
        assert limiter.check().allowed is True
        assert limiter.check().allowed is False


class TestGetLlmCallLimiter:
    def test_returns_the_same_instance_on_repeated_calls(self):
        first = get_llm_call_limiter(20)
        second = get_llm_call_limiter(20)
        assert first is second

    def test_state_persists_across_calls_since_it_is_a_singleton(self):
        limiter = get_llm_call_limiter(20)
        limiter.reset()
        limiter.check(now=0.0)

        # Re-fetching returns the same object with the event already recorded.
        same_limiter = get_llm_call_limiter(20)
        assert len(same_limiter._events) == 1  # noqa: SLF001 - white-box check, test-only

        limiter.reset()


class TestMessages:
    def test_question_and_llm_call_messages_are_distinct_and_calm(self):
        """Different wording for the two limiters (submission-time vs.
        mid-retry-loop) -- both non-technical, no raw counters/exceptions."""
        assert QUESTION_LIMIT_MESSAGE != LLM_CALL_LIMIT_MESSAGE
        for message in (QUESTION_LIMIT_MESSAGE, LLM_CALL_LIMIT_MESSAGE):
            assert "Exception" not in message
            assert "Traceback" not in message
