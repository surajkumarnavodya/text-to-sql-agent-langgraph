"""Reduces a list of `CaseRunResult`s into the benchmark's named metrics.

Every metric below is a straightforward reduction over `CaseRunResult`
fields that `eval/evaluators.py` (correctness verdicts) and `eval/runner.py`
(timing/cost/retrieval/token raw signal) already populated -- this module
does no evaluation of its own, only aggregation, so a metric's definition
lives in exactly one place.

Naming note on metrics #4/#5 ("schema retrieval recall" / "relevant-table
recall"): the runtime's `retrieve_schema_node` only exposes the *final*
retrieved table set in `AgentState` (post FK-adjacency-bridge-expansion,
see `agent/graph.py`'s docstring) -- there's no separate "raw top-k, before
bridging" value to measure independently without changing production
agent code for an eval-only need. This framework instead reports the two
distinct, genuinely useful angles computable from that one set:
**recall** (of the tables a question needed, how many were retrieved --
`schema_retrieval_recall`) and **precision** (of the tables retrieved, how
many were actually needed -- `relevant_table_precision`, catching
"retrieved a pile of irrelevant tables" separately from "missed a needed
one"). Documented here explicitly so the mapping from the requested metric
names is never ambiguous.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from eval.schema import BenchmarkReport, CaseRunResult

# Metrics computed only over cases where the relevant signal is applicable
# (e.g. join_correctness only over "joins"-category cases) -- a category
# with zero matching cases in a given run reports None, not 0.0 (which
# would misleadingly read as "0% accuracy" rather than "not measured").


def _rate(results: list[CaseRunResult], predicate) -> float | None:
    applicable = [r for r in results if predicate(r) is not None]
    if not applicable:
        return None
    return sum(1 for r in applicable if predicate(r)) / len(applicable)


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(round(pct / 100 * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def _category_results(results: list[CaseRunResult], category: str) -> list[CaseRunResult]:
    return [r for r in results if r.category == category]


def _structure_check_accuracy(
    results: list[CaseRunResult], category: str, check_name: str
) -> float | None:
    values = [
        r.structure_checks[check_name]
        for r in _category_results(results, category)
        if check_name in r.structure_checks
    ]
    if not values:
        return None
    return sum(1 for v in values if v) / len(values)


def compute_metrics(results: list[CaseRunResult]) -> dict[str, float | None]:
    """Computes every named benchmark metric over `results`.

    Args:
        results: One `CaseRunResult` per standalone case and per follow-up
            turn from a completed (or partially completed) benchmark run.

    Returns:
        A flat dict of metric name -> value (0.0-1.0 for accuracy/recall
        metrics, seconds for latency, a raw count/average otherwise). A
        value of None means "not applicable / no matching cases in this
        run" -- callers (`eval/reporting.py`) must render that distinctly
        from 0.0, never silently coerce it.
    """
    accuracy_cases = [r for r in results if r.security_classification != "adversarial"]
    adversarial_cases = [r for r in results if r.security_classification == "adversarial"]
    non_adversarial_succeed_attempts = [
        r for r in accuracy_cases if r.final_status in ("succeeded", "failed", "rate_limited")
    ]

    latencies = [r.wall_time_seconds for r in results if r.wall_time_seconds]
    llm_calls = [r.llm_call_count for r in results]
    complexities = [r.complexity_score for r in results if r.complexity_score is not None]
    cost_rows = [r.cost_estimate_rows for r in results if r.cost_estimate_rows is not None]

    retried_cases = [r for r in accuracy_cases if r.retry_count > 0]
    first_attempt_cases = [
        r
        for r in accuracy_cases
        if r.retry_count == 0 and r.final_status in ("succeeded", "failed")
    ]

    followup_results = [r for r in results if r.turn_index is not None]

    metrics: dict[str, float | None] = {
        # 1-3: SQL/result correctness
        "sql_execution_accuracy": _rate(
            non_adversarial_succeed_attempts, lambda r: r.final_status == "succeeded"
        ),
        "result_set_accuracy": _rate(accuracy_cases, lambda r: r.result_set_correct),
        "exact_sql_match": _rate(accuracy_cases, lambda r: r.sql_exact_match),
        "final_accuracy": _rate(accuracy_cases, lambda r: r.overall_pass),
        # 4-6: retrieval + column selection
        "schema_retrieval_recall": _mean(
            [r.retrieval_recall for r in accuracy_cases if r.retrieval_recall is not None]
        ),
        "relevant_table_precision": _mean(
            [
                len(
                    set(t.lower() for t in r.expected_tables_hint)
                    & set(t.lower() for t in r.retrieved_tables)
                )
                / len(r.retrieved_tables)
                for r in accuracy_cases
                if getattr(r, "expected_tables_hint", None) and r.retrieved_tables
            ]
        ),
        "column_selection_accuracy": _mean(
            [r.column_recall for r in accuracy_cases if r.column_recall is not None]
        ),
        # 7-15: structural correctness, per category
        "join_correctness": _mean(
            [
                v
                for cat in ("joins", "complex_joins", "multi_table_analysis")
                for v in (
                    _structure_check_accuracy(results, cat, "join_tables_correct"),
                    _structure_check_accuracy(results, cat, "join_present"),
                )
                if v is not None
            ]
        ),
        "aggregation_correctness": _mean(
            [
                v
                for cat in ("aggregation", "conditional_aggregation")
                for v in (_structure_check_accuracy(results, cat, "aggregation_present"),)
                if v is not None
            ]
        ),
        "filter_correctness": _structure_check_accuracy(
            results, "simple_filtering", "filter_present"
        ),
        "date_time_reasoning_accuracy": _structure_check_accuracy(
            results, "date_filtering", "date_condition_present"
        ),
        "group_by_correctness": _structure_check_accuracy(
            results, "group_by_having", "group_by_present"
        ),
        "order_by_correctness": _structure_check_accuracy(results, "sorting", "order_by_present"),
        "null_handling_accuracy": _structure_check_accuracy(
            results, "null_handling", "null_handling_present"
        ),
        "nested_query_correctness": _structure_check_accuracy(
            results, "nested_queries", "nested_query_present"
        ),
        "window_function_correctness": _structure_check_accuracy(
            results, "window_functions", "window_function_present"
        ),
        # 16-17: real-world handling
        "ambiguous_question_handling_accuracy": _rate(
            _category_results(results, "ambiguous_wording"), lambda r: r.overall_pass
        ),
        "follow_up_accuracy": _rate(followup_results, lambda r: r.overall_pass),
        # 18-19: retry behavior
        "retry_success_rate": _rate(retried_cases, lambda r: r.final_status == "succeeded"),
        "first_attempt_success_rate": _rate(
            first_attempt_cases, lambda r: r.final_status == "succeeded"
        ),
        # 20-23: latency / LLM-call / token cost
        "average_latency_seconds": _mean(latencies),
        "p95_latency_seconds": _percentile(latencies, 95),
        "avg_llm_calls_per_case": _mean([float(c) for c in llm_calls]),
        "total_llm_calls": float(sum(llm_calls)) if llm_calls else None,
        "avg_prompt_tokens": _mean(
            [r.prompt_tokens for r in results if getattr(r, "prompt_tokens", None) is not None]
        ),
        "avg_completion_tokens": _mean(
            [
                r.completion_tokens
                for r in results
                if getattr(r, "completion_tokens", None) is not None
            ]
        ),
        # 24-25: complexity / cost
        "avg_query_complexity": _mean([float(c) for c in complexities]),
        "avg_estimated_cost_rows": _mean(cost_rows),
        # 26: security
        "security_rejection_accuracy": _rate(adversarial_cases, lambda r: r.security_correct),
    }
    return metrics


def compute_breakdown(results: list[CaseRunResult], key: str) -> dict[str, dict[str, float]]:
    """Groups `results` by `category` or `difficulty` and computes a small
    per-group summary (pass rate, count, avg latency) -- the report's
    per-category/per-difficulty tables.
    """
    groups: dict[str, list[CaseRunResult]] = defaultdict(list)
    for r in results:
        groups[getattr(r, key)].append(r)

    breakdown: dict[str, dict[str, float]] = {}
    for name, group in sorted(groups.items()):
        passed = sum(1 for r in group if r.overall_pass)
        latencies = [r.wall_time_seconds for r in group if r.wall_time_seconds]
        breakdown[name] = {
            "count": float(len(group)),
            "pass_rate": passed / len(group) if group else 0.0,
            "avg_latency_seconds": statistics.mean(latencies) if latencies else 0.0,
        }
    return breakdown


def build_report(
    run_id: str,
    timestamp: str,
    model: str,
    database: str,
    results: list[CaseRunResult],
) -> BenchmarkReport:
    """Assembles the full `BenchmarkReport` from a completed run's raw results."""
    return BenchmarkReport(
        run_id=run_id,
        timestamp=timestamp,
        model=model,
        database=database,
        total_cases=len(results),
        metrics=compute_metrics(results),
        per_category=compute_breakdown(results, "category"),
        per_difficulty=compute_breakdown(results, "difficulty"),
        results=results,
    )
