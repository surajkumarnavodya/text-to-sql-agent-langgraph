"""Renders a `BenchmarkReport` as markdown, and (de)serializes it to/from
JSON for baseline storage (see `eval/regression.py`).

The JSON form deliberately stores a *compact* summary -- metrics plus one
pass/fail + a short outcome per case -- not the full raw result rows/SQL
text for every case. That keeps a committed baseline file small and free of
database content, while still being enough to detect exactly which case(s)
flipped from passing to failing (see `eval/regression.py`), not just that
"some aggregate metric moved."
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from eval.schema import BenchmarkReport, CaseRunResult

_METRIC_LABELS: dict[str, str] = {
    "sql_execution_accuracy": "SQL execution accuracy",
    "result_set_accuracy": "Result-set accuracy",
    "exact_sql_match": "Exact SQL match (diagnostic only)",
    "final_accuracy": "Final accuracy",
    "schema_retrieval_recall": "Schema retrieval recall",
    "relevant_table_precision": "Relevant-table precision",
    "column_selection_accuracy": "Column selection accuracy",
    "join_correctness": "Join correctness",
    "aggregation_correctness": "Aggregation correctness",
    "filter_correctness": "Filter correctness",
    "date_time_reasoning_accuracy": "Date/time reasoning accuracy",
    "group_by_correctness": "GROUP BY correctness",
    "order_by_correctness": "ORDER BY correctness",
    "null_handling_accuracy": "NULL handling accuracy",
    "nested_query_correctness": "Nested-query correctness",
    "window_function_correctness": "Window-function correctness",
    "ambiguous_question_handling_accuracy": "Ambiguous-question handling accuracy",
    "follow_up_accuracy": "Follow-up question accuracy",
    "retry_success_rate": "Retry success rate",
    "first_attempt_success_rate": "First-attempt (first-pass) accuracy",
    "average_latency_seconds": "Average latency",
    "p95_latency_seconds": "P95 latency",
    "avg_llm_calls_per_case": "Avg. LLM calls per question",
    "total_llm_calls": "Total LLM calls",
    "avg_prompt_tokens": "Avg. prompt tokens",
    "avg_completion_tokens": "Avg. completion tokens",
    "avg_query_complexity": "Avg. query complexity score",
    "avg_estimated_cost_rows": "Avg. estimated query cost (rows)",
    "security_rejection_accuracy": "Security rejection accuracy",
}

# Order matches the benchmark request's headline summary shape.
_HEADLINE_METRICS = (
    "first_attempt_success_rate",
    "final_accuracy",
    "schema_retrieval_recall",
    "join_correctness",
    "aggregation_correctness",
    "follow_up_accuracy",
    "security_rejection_accuracy",
)


def _fmt(value: float | None, *, as_percent: bool, unit: str = "") -> str:
    if value is None:
        return "n/a (no applicable cases in this run)"
    if as_percent:
        return f"{value * 100:.1f}%"
    return f"{value:.2f}{unit}"


_PERCENT_METRICS = {
    "sql_execution_accuracy",
    "result_set_accuracy",
    "exact_sql_match",
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
_SECONDS_METRICS = {"average_latency_seconds", "p95_latency_seconds"}


def render_headline(report: BenchmarkReport) -> str:
    """The short summary block, matching the benchmark request's example shape."""
    lines = ["## Text-to-SQL Evaluation", "", f"Total questions: {report.total_cases}"]
    for key in _HEADLINE_METRICS:
        label = _METRIC_LABELS[key]
        value = report.metrics.get(key)
        lines.append(f"{label}: {_fmt(value, as_percent=True)}")
    lines.append(
        f"Average latency: {_fmt(report.metrics.get('average_latency_seconds'), as_percent=False, unit=' sec')}"
    )
    lines.append(
        f"P95 latency: {_fmt(report.metrics.get('p95_latency_seconds'), as_percent=False, unit=' sec')}"
    )
    return "\n".join(lines)


def render_full_report(report: BenchmarkReport) -> str:
    """Full markdown report: headline, complete metrics table, per-category/
    per-difficulty breakdowns, and a failure-analysis section listing every
    failed case with its classified likely cause (per the benchmark
    request's explicit "identify the questions that failed" requirement).
    """
    sections = [render_headline(report), ""]

    sections.append(
        f"**Run:** `{report.run_id}` | **Model:** `{report.model}` | "
        f"**Database:** `{report.database}` | **Timestamp:** {report.timestamp}"
    )
    sections.append("")

    sections.append("### All metrics")
    sections.append("")
    sections.append("| Metric | Value |")
    sections.append("|---|---|")
    for key, label in _METRIC_LABELS.items():
        value = report.metrics.get(key)
        as_percent = key in _PERCENT_METRICS
        as_seconds = key in _SECONDS_METRICS
        rendered = _fmt(value, as_percent=as_percent, unit=" sec" if as_seconds else "")
        sections.append(f"| {label} | {rendered} |")
    sections.append("")

    sections.append("### By difficulty")
    sections.append("")
    sections.append("| Difficulty | Count | Pass rate | Avg latency (s) |")
    sections.append("|---|---|---|---|")
    for name, stats in report.per_difficulty.items():
        sections.append(
            f"| {name} | {int(stats['count'])} | {stats['pass_rate'] * 100:.1f}% | "
            f"{stats['avg_latency_seconds']:.1f} |"
        )
    sections.append("")

    sections.append("### By category")
    sections.append("")
    sections.append("| Category | Count | Pass rate | Avg latency (s) |")
    sections.append("|---|---|---|---|")
    for name, stats in report.per_category.items():
        sections.append(
            f"| {name} | {int(stats['count'])} | {stats['pass_rate'] * 100:.1f}% | "
            f"{stats['avg_latency_seconds']:.1f} |"
        )
    sections.append("")

    failures = report.failures
    sections.append(f"### Failed questions ({len(failures)})")
    sections.append("")
    if not failures:
        sections.append("None.")
    else:
        for r in failures:
            turn_label = f" (turn {r.turn_index + 1})" if r.turn_index is not None else ""
            sections.append(f"- **[{r.case_id}{turn_label}]** {r.question!r}")
            sections.append(
                f"  - difficulty={r.difficulty} category={r.category} status={r.final_status}"
            )
            sections.append(
                f"  - likely failure category: `{r.failure_category}` -- {r.error_detail}"
            )
            if r.generated_sql:
                sections.append(f"  - generated SQL: `{r.generated_sql}`")
    sections.append("")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# JSON (de)serialization -- compact, for baseline storage
# ---------------------------------------------------------------------------


def _case_summary(r: CaseRunResult) -> dict:
    return {
        "case_id": r.case_id,
        "turn_index": r.turn_index,
        "difficulty": r.difficulty,
        "category": r.category,
        "overall_pass": r.overall_pass,
        "final_status": r.final_status,
        "failure_category": r.failure_category,
    }


def report_to_dict(report: BenchmarkReport) -> dict:
    """Compact JSON-serializable form -- metrics + a per-case pass/fail
    summary, not the full raw rows/SQL (see module docstring)."""
    return {
        "run_id": report.run_id,
        "timestamp": report.timestamp,
        "model": report.model,
        "database": report.database,
        "total_cases": report.total_cases,
        "metrics": report.metrics,
        "per_category": report.per_category,
        "per_difficulty": report.per_difficulty,
        "cases": [_case_summary(r) for r in report.results],
    }


def save_report_json(report: BenchmarkReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_to_dict(report), indent=2), encoding="utf-8")


def save_full_results_json(report: BenchmarkReport, path: Path) -> None:
    """Saves the *full* per-case detail (including generated SQL and result
    rows) -- separate from `save_report_json`'s compact baseline form, for
    when you want to inspect exactly what the model produced on a given
    run. Not intended to be committed to version control (may contain real
    row data from the connected database)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report_to_dict(report)
    payload["cases_full"] = [asdict(r) for r in report.results]
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def load_report_dict(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
