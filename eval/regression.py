"""Compares a benchmark run against a stored baseline and flags regressions.

Two independent signals, since either alone can miss what the other catches:

1. **Metric-level drift** -- did a named metric (accuracy/recall: lower is
   worse; latency/cost/complexity: higher is worse) move beyond a tolerance
   band. Catches a broad, gradual degradation across many cases.
2. **Per-case flips** -- did a *specific* case that passed in the baseline
   now fail (or vice versa). Catches a narrow, sharp regression on one
   case that's too small to move an aggregate metric outside its
   tolerance band -- e.g. one join-correctness case flipping in a run of
   200 barely dents the aggregate `join_correctness` percentage, but it's
   still a real, specific regression worth surfacing by name.

This module contains no live-agent dependency -- it operates purely on
already-computed `BenchmarkReport`/baseline-dict data, which is what makes
`tests/test_eval_regression.py` able to test it fully offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eval.schema import BenchmarkReport

# Metrics where a LOWER value is worse (accuracy/recall/precision-style,
# 0.0-1.0 scaled) vs. metrics where a HIGHER value is worse (latency, LLM
# call count, token usage, complexity, estimated cost) -- direction matters
# for deciding which side of "moved" counts as a regression.
_LOWER_IS_WORSE: frozenset[str] = frozenset(
    {
        "sql_execution_accuracy",
        "result_set_accuracy",
        "final_accuracy",
        "schema_retrieval_recall",
        "relevant_table_precision",
        "column_selection_accuracy",
        "join_correctness",
        "aggregation_correctness",
        "filter_correctness",
        "date_time_reasoning_accuracy",
        "group_by_correctness",
        "order_by_correctness",
        "null_handling_accuracy",
        "nested_query_correctness",
        "window_function_correctness",
        "ambiguous_question_handling_accuracy",
        "follow_up_accuracy",
        "retry_success_rate",
        "first_attempt_success_rate",
        "security_rejection_accuracy",
    }
)
_HIGHER_IS_WORSE: frozenset[str] = frozenset(
    {
        "average_latency_seconds",
        "p95_latency_seconds",
        "avg_llm_calls_per_case",
        "total_llm_calls",
        "avg_prompt_tokens",
        "avg_completion_tokens",
        "avg_query_complexity",
        "avg_estimated_cost_rows",
    }
)

# Absolute tolerance for 0.0-1.0-scaled accuracy/recall metrics (3
# percentage points) -- small enough to catch a real regression, large
# enough that ordinary LLM sampling noise across a run doesn't trip it
# every time (this project's generation calls use temperature=0.0 -- see
# agent/llm_client.py -- so run-to-run variance should be low but not
# necessarily exactly zero, since retrieval/schema-index state can still
# shift results at the margin).
DEFAULT_ACCURACY_TOLERANCE = 0.03
# Relative tolerance for unbounded metrics (latency, tokens, cost, ...) --
# 20% slower/more expensive before it's flagged, since these are
# inherently noisier (machine load, model warm state) than a determinism-
# seeking accuracy metric.
DEFAULT_RELATIVE_TOLERANCE = 0.20


@dataclass
class MetricRegression:
    metric: str
    baseline_value: float
    current_value: float
    direction: str  # "lower_is_worse" | "higher_is_worse"


@dataclass
class RegressionReport:
    metric_regressions: list[MetricRegression] = field(default_factory=list)
    newly_failing_cases: list[str] = field(default_factory=list)
    newly_passing_cases: list[str] = field(default_factory=list)

    @property
    def has_regression(self) -> bool:
        return bool(self.metric_regressions or self.newly_failing_cases)

    def summary(self) -> str:
        if not self.has_regression:
            return "No regression detected."
        lines = []
        if self.metric_regressions:
            lines.append("Metric regressions:")
            for r in self.metric_regressions:
                lines.append(
                    f"  - {r.metric}: baseline={r.baseline_value:.3f} -> "
                    f"current={r.current_value:.3f} ({r.direction})"
                )
        if self.newly_failing_cases:
            lines.append(f"Newly failing case(s): {', '.join(self.newly_failing_cases)}")
        if self.newly_passing_cases:
            lines.append(
                f"Newly passing case(s) (not a regression, noted for visibility): "
                f"{', '.join(self.newly_passing_cases)}"
            )
        return "\n".join(lines)


def detect_regression(
    baseline: dict,
    current: BenchmarkReport,
    accuracy_tolerance: float = DEFAULT_ACCURACY_TOLERANCE,
    relative_tolerance: float = DEFAULT_RELATIVE_TOLERANCE,
) -> RegressionReport:
    """Compares `current` against `baseline` (as produced by
    `eval.reporting.report_to_dict`) and returns every detected regression.

    Args:
        baseline: A previously-saved baseline dict (see
            `eval.reporting.save_report_json`/`load_report_dict`).
        current: The just-completed run's report.
        accuracy_tolerance: Absolute tolerance for 0.0-1.0-scaled metrics.
        relative_tolerance: Relative (fractional) tolerance for unbounded
            metrics (latency, tokens, cost, complexity).

    Returns:
        A `RegressionReport` -- `has_regression` is True if anything
        meaningful moved in the worse direction.
    """
    result = RegressionReport()
    baseline_metrics: dict[str, float | None] = baseline.get("metrics", {})

    for metric, baseline_value in baseline_metrics.items():
        if baseline_value is None:
            continue
        current_value = current.metrics.get(metric)
        if current_value is None:
            continue
        if metric in _LOWER_IS_WORSE:
            if current_value < baseline_value - accuracy_tolerance:
                result.metric_regressions.append(
                    MetricRegression(metric, baseline_value, current_value, "lower_is_worse")
                )
        elif metric in _HIGHER_IS_WORSE:
            threshold = baseline_value * (1 + relative_tolerance)
            if current_value > threshold:
                result.metric_regressions.append(
                    MetricRegression(metric, baseline_value, current_value, "higher_is_worse")
                )

    baseline_case_pass: dict[str, bool] = {
        _case_key(c): c["overall_pass"] for c in baseline.get("cases", [])
    }
    current_case_pass: dict[str, bool] = {
        _case_key({"case_id": r.case_id, "turn_index": r.turn_index}): r.overall_pass
        for r in current.results
    }
    for key, was_passing in baseline_case_pass.items():
        now_passing = current_case_pass.get(key)
        if now_passing is None:
            continue  # case removed/renamed since baseline -- not a regression signal
        if was_passing and not now_passing:
            result.newly_failing_cases.append(key)
        elif not was_passing and now_passing:
            result.newly_passing_cases.append(key)

    return result


def _case_key(case: dict) -> str:
    turn = case.get("turn_index")
    return case["case_id"] if turn is None else f"{case['case_id']}#turn{turn}"
