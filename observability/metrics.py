"""Live, in-process rollup of per-stage LangGraph node timings.

`agent.nodes._timed_node` has, since before this module existed, logged a
`[timing] stage=... attempt=... duration_ms=...` line for every node call
and appended a `StageTiming` entry to `AgentState["stage_timings"]`. That
data was previously only ever consumed two ways: read live off a log line
by a human, or aggregated *offline* from a captured `eval/results/run_*
.json` file by `docs/PERFORMANCE_BASELINE.md`'s manual analysis. Neither
answers "how is this deployment performing right now" without either
grepping logs by hand or running the benchmark harness.

This module closes that gap with the smallest possible addition: a
bounded, thread-safe, in-process rolling window that `agent.graph.run_agent`
feeds after every completed run (see that module's own call to
`record_agent_run`), and `GET /metrics/performance` (`api/main.py`) reads
back as a live snapshot. No new per-node instrumentation was added -- this
purely aggregates data `_timed_node` already produces.

**Deliberate limits, stated up front, matching `agent.rate_limit`'s own
disclosed posture for the same reason:**
- **Single-process, in-memory only.** A multi-worker deployment (several
  `uvicorn` processes) would have one independent rollup per process, not
  one merged view -- exactly the same limitation `agent/rate_limit.py`'s
  own module docstring already discloses for its sliding-window limiters,
  and for the same reason (no shared store like Redis is part of this
  application's current architecture). Real cross-process metrics would
  need Prometheus/OpenTelemetry or an equivalent, which is a genuinely
  separate, larger piece of infrastructure -- not attempted here.
- **Resets on process restart.** By design -- this is a live operational
  view, not a durable metrics store. `docs/PERFORMANCE_BASELINE.md`'s own
  captured-benchmark analysis remains the durable, point-in-time record;
  this module is for "what is happening right now," not history.
- **Bounded by request count, not time.** The rolling window keeps the
  most recent `max_requests` (default 500) completed runs' timings, not a
  fixed time window -- simpler and memory-bounded regardless of traffic
  rate.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from functools import cache
from typing import TypedDict

from agent.state import StageTiming

_DEFAULT_MAX_REQUESTS = 500


class StageSummary(TypedDict):
    """One stage's rolled-up timing distribution across the current window.

    Mirrors the exact column shape `docs/PERFORMANCE_BASELINE.md`'s manual
    per-stage table already established (n/mean/P50/P95/total), so a live
    snapshot from this module and that document's offline analysis read
    the same way.
    """

    stage: str
    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    total_ms: float


class RequestSummary(TypedDict):
    """Rolled-up total-request-duration distribution across the window."""

    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float


class MetricsSnapshot(TypedDict):
    """The full live rollup, as returned by `GET /metrics/performance`."""

    started_at: str
    window_requests: int
    max_window_requests: int
    requests: RequestSummary
    stages: list[StageSummary]
    status_counts: dict[str, int]


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted list.

    Deliberately not `statistics.quantiles` -- that raises on fewer than
    two data points, which a freshly-started (or very low-traffic) process
    hits immediately, and `MetricsSnapshot` must stay well-defined (0.0,
    not an exception) even with a single sample or none at all.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = min(int(len(sorted_values) * fraction), len(sorted_values) - 1)
    return sorted_values[index]


class PerformanceMetrics:
    """Bounded, thread-safe rolling window of stage/request timings.

    `record_agent_run` is the sole write path, called once per completed
    `agent.graph.run_agent` invocation; `snapshot` is the sole read path,
    called by `GET /metrics/performance`. Both are cheap (a lock held only
    long enough to append/copy bounded deques) so neither adds meaningful
    latency to the request path they instrument -- consistent with this
    codebase's existing `agent.rate_limit.SlidingWindowRateLimiter`, which
    makes the identical tradeoff for the identical reason.
    """

    def __init__(self, max_requests: int = _DEFAULT_MAX_REQUESTS) -> None:
        self._lock = threading.Lock()
        self._max_requests = max_requests
        self._started_at = time.time()
        self._request_durations_ms: deque[float] = deque(maxlen=max_requests)
        self._stage_durations_ms: dict[str, deque[float]] = {}
        self._status_counts: dict[str, int] = {}

    def record_agent_run(
        self,
        stage_timings: list[StageTiming],
        total_duration_ms: float,
        status: str | None,
    ) -> None:
        """Feeds one completed `run_agent()` call's timings into the window.

        Never raises -- a malformed/empty `stage_timings` (e.g. a rejected
        question that never reached any node) degrades to "no stage data
        recorded for this request," not a broken request. Matches this
        codebase's "an accuracy/observability aid must never be a reason a
        question can't be answered" posture (the same principle
        `agent.llm_client._build_golden_examples_block`'s own docstring
        states for a different subsystem).
        """
        with self._lock:
            self._request_durations_ms.append(total_duration_ms)
            self._status_counts[status or "unknown"] = (
                self._status_counts.get(status or "unknown", 0) + 1
            )
            for timing in stage_timings:
                stage = timing.get("stage")
                duration = timing.get("duration_ms")
                if not stage or duration is None:
                    continue
                bucket = self._stage_durations_ms.setdefault(
                    stage, deque(maxlen=self._max_requests)
                )
                bucket.append(duration)

    def snapshot(self) -> MetricsSnapshot:
        """Returns the current rollup. Cheap enough to call on every poll --
        computes percentiles over at most `max_requests` samples per stage."""
        with self._lock:
            request_durations = sorted(self._request_durations_ms)
            stage_items = [
                (stage, sorted(durations)) for stage, durations in self._stage_durations_ms.items()
            ]
            status_counts = dict(self._status_counts)
            window_requests = len(self._request_durations_ms)

        requests: RequestSummary = {
            "count": len(request_durations),
            "mean_ms": round(statistics.fmean(request_durations), 2) if request_durations else 0.0,
            "p50_ms": round(_percentile(request_durations, 0.50), 2),
            "p95_ms": round(_percentile(request_durations, 0.95), 2),
            "max_ms": round(request_durations[-1], 2) if request_durations else 0.0,
        }

        stages: list[StageSummary] = []
        for stage, durations in sorted(stage_items, key=lambda item: sum(item[1]), reverse=True):
            stages.append(
                {
                    "stage": stage,
                    "count": len(durations),
                    "mean_ms": round(statistics.fmean(durations), 2),
                    "p50_ms": round(_percentile(durations, 0.50), 2),
                    "p95_ms": round(_percentile(durations, 0.95), 2),
                    "max_ms": round(durations[-1], 2),
                    "total_ms": round(sum(durations), 2),
                }
            )

        return {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._started_at)),
            "window_requests": window_requests,
            "max_window_requests": self._max_requests,
            "requests": requests,
            "stages": stages,
            "status_counts": status_counts,
        }


@cache
def get_default_metrics() -> PerformanceMetrics:
    """The process-wide singleton `agent.graph.run_agent` writes to and
    `GET /metrics/performance` reads from -- same `functools.cache`
    singleton pattern as every other process-lifetime object in this
    codebase (`db.connection._cached_engine`, `agent.llm_client
    ._get_ollama_client`, `agent.graph.build_graph`, etc.), not a new
    pattern introduced for this module."""
    return PerformanceMetrics()
