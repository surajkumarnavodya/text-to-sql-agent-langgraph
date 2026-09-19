"""Pure aggregation-logic tests for `observability.metrics` -- no LLM, no
database, no HTTP client needed, since `PerformanceMetrics` only ever
consumes plain `StageTiming`-shaped dicts and floats. Endpoint-level
wiring/authorization is covered separately in
`tests/test_api_authz.py::test_admin_can_view_performance_metrics` /
`test_analyst_cannot_view_performance_metrics`.
"""

from __future__ import annotations

from typing import Any, cast

from agent.state import StageTiming
from observability.metrics import PerformanceMetrics, get_default_metrics


def _timing(stage: str, duration_ms: float, attempt: int = 1) -> StageTiming:
    return {"stage": stage, "attempt": attempt, "duration_ms": duration_ms}


class TestEmptySnapshot:
    def test_a_fresh_instance_returns_zeroed_summaries_not_an_exception(self):
        metrics = PerformanceMetrics()

        snapshot = metrics.snapshot()

        assert snapshot["window_requests"] == 0
        assert snapshot["requests"] == {
            "count": 0,
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "max_ms": 0.0,
        }
        assert snapshot["stages"] == []
        assert snapshot["status_counts"] == {}


class TestRecordingAndAggregation:
    def test_one_recorded_run_is_reflected_in_both_request_and_stage_summaries(self):
        metrics = PerformanceMetrics()

        metrics.record_agent_run(
            [_timing("generate_sql", 1000.0), _timing("retrieve_schema", 200.0)],
            total_duration_ms=1500.0,
            status="succeeded",
        )
        snapshot = metrics.snapshot()

        assert snapshot["window_requests"] == 1
        assert snapshot["requests"]["count"] == 1
        assert snapshot["requests"]["mean_ms"] == 1500.0
        assert snapshot["status_counts"] == {"succeeded": 1}
        stage_names = {stage["stage"] for stage in snapshot["stages"]}
        assert stage_names == {"generate_sql", "retrieve_schema"}

    def test_stages_are_ranked_by_total_time_descending(self):
        """Mirrors docs/PERFORMANCE_BASELINE.md's own table ordering --
        the largest-share stage (generate_sql, the LLM call) should sort
        first, matching what a human reading the rollup expects."""
        metrics = PerformanceMetrics()

        metrics.record_agent_run(
            [_timing("generate_sql", 5000.0), _timing("validate_sql", 10.0)],
            total_duration_ms=5010.0,
            status="succeeded",
        )
        metrics.record_agent_run(
            [_timing("generate_sql", 3000.0), _timing("validate_sql", 12.0)],
            total_duration_ms=3012.0,
            status="succeeded",
        )

        stages = metrics.snapshot()["stages"]

        assert [s["stage"] for s in stages] == ["generate_sql", "validate_sql"]
        assert stages[0]["count"] == 2
        assert stages[0]["total_ms"] == 8000.0
        assert stages[0]["max_ms"] == 5000.0

    def test_multiple_calls_to_the_same_stage_in_one_run_are_both_recorded(self):
        """A question that retries generate_sql twice produces two entries
        for that stage in one run -- accumulate all of them, not just the
        last (same principle `agent.state.StageTiming`'s own docstring
        states: "lets a profiling script sum total time... including
        retries")."""
        metrics = PerformanceMetrics()

        metrics.record_agent_run(
            [
                _timing("generate_sql", 1000.0, attempt=1),
                _timing("generate_sql", 1200.0, attempt=2),
            ],
            total_duration_ms=2200.0,
            status="succeeded",
        )

        stages = metrics.snapshot()["stages"]

        assert stages[0]["count"] == 2
        assert stages[0]["total_ms"] == 2200.0

    def test_percentiles_are_computed_over_the_recorded_distribution(self):
        metrics = PerformanceMetrics()
        for value in [10.0, 20.0, 30.0, 40.0, 100.0]:
            metrics.record_agent_run(
                [_timing("generate_sql", value)], total_duration_ms=value, status="succeeded"
            )

        stage = metrics.snapshot()["stages"][0]

        assert stage["max_ms"] == 100.0
        assert stage["mean_ms"] == 40.0
        # p95 with 5 samples: index = min(int(5*0.95), 4) = 4 -> the max
        assert stage["p95_ms"] == 100.0

    def test_status_counts_tally_across_multiple_outcomes(self):
        metrics = PerformanceMetrics()
        metrics.record_agent_run([], total_duration_ms=5.0, status="rejected")
        metrics.record_agent_run([], total_duration_ms=10.0, status="succeeded")
        metrics.record_agent_run([], total_duration_ms=8.0, status="succeeded")

        status_counts = metrics.snapshot()["status_counts"]

        assert status_counts == {"rejected": 1, "succeeded": 2}

    def test_a_run_with_no_stage_timings_still_records_its_total_duration(self):
        """An input-rejected question (sanitize_input never even runs the
        _timed_node-wrapped path for later stages) still has a real total
        duration worth recording, even with an empty stage list."""
        metrics = PerformanceMetrics()

        metrics.record_agent_run([], total_duration_ms=3.0, status="rejected")

        snapshot = metrics.snapshot()
        assert snapshot["requests"]["count"] == 1
        assert snapshot["stages"] == []


class TestBoundedWindow:
    def test_the_window_evicts_the_oldest_entries_once_max_requests_is_exceeded(self):
        metrics = PerformanceMetrics(max_requests=3)

        for i in range(5):
            metrics.record_agent_run(
                [_timing("generate_sql", float(i))], total_duration_ms=float(i), status="succeeded"
            )

        snapshot = metrics.snapshot()

        # Only the 3 most recent request durations (2, 3, 4) survive.
        assert snapshot["window_requests"] == 3
        assert snapshot["requests"]["max_ms"] == 4.0
        assert snapshot["stages"][0]["count"] == 3
        assert snapshot["stages"][0]["max_ms"] == 4.0


class TestFailOpen:
    def test_malformed_stage_timing_entries_are_skipped_not_raised(self):
        metrics = PerformanceMetrics()

        # Missing "duration_ms", missing "stage", and a well-formed entry
        # in the same call -- the well-formed one must still be recorded.
        # Deliberately malformed relative to StageTiming's declared shape
        # (that's the point of this test -- real callers get this from
        # LangGraph state merging, which isn't type-checked at runtime),
        # so the malformed entries are cast rather than fixed to satisfy
        # the type checker while keeping the actual runtime shape wrong.
        malformed_entries: list[Any] = [{"stage": "generate_sql"}, {"duration_ms": 5.0}]
        metrics.record_agent_run(
            cast(list[StageTiming], malformed_entries) + [_timing("validate_sql", 12.0)],
            total_duration_ms=20.0,
            status="succeeded",
        )

        stages = metrics.snapshot()["stages"]

        assert [s["stage"] for s in stages] == ["validate_sql"]

    def test_a_none_status_is_recorded_under_unknown_rather_than_crashing(self):
        metrics = PerformanceMetrics()

        metrics.record_agent_run([], total_duration_ms=1.0, status=None)

        assert metrics.snapshot()["status_counts"] == {"unknown": 1}


class TestDefaultSingleton:
    def test_get_default_metrics_returns_the_same_instance_every_call(self):
        assert get_default_metrics() is get_default_metrics()
