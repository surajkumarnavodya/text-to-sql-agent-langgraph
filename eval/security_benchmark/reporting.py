"""Renders a `SecurityBenchmarkReport` as markdown, and (de)serializes it to
JSON -- mirrors `eval/reporting.py`'s split between a compact, commit-safe
summary and a full, evidence-carrying results file.

The full-results JSON is **more sensitive than `eval/reporting.py`'s
equivalent**: a genuine finding (a leaked secret label, a leaked system-
prompt fingerprint, an unauthorized source) is exactly what it exists to
capture, and `response_text_excerpt` on a failing case may contain real
leaked content. Never commit `save_full_results_json`'s output to version
control -- the same warning `eval/reporting.py`'s own docstring already
gives its equivalent, stated more forcefully here because the content is
adversarial-by-construction rather than incidental (ordinary query result
rows).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from eval.security_benchmark.schema import SecurityBenchmarkReport, SecurityCaseResult


def render_headline(report: SecurityBenchmarkReport) -> str:
    lines = [
        "## Prompt-Injection Security Benchmark",
        "",
        f"Total cases: {report.total_cases}",
        f"Caller role(s) tested: {', '.join(report.caller_roles) or '(none)'}",
        f"Overall pass rate: {_pct(report.metrics.get('pass_rate'))}",
        f"Pass rate (deduplicated by unique payload): {_pct(report.metrics.get('dedup_pass_rate'))}",
        f"Expected-behavior match rate: {_pct(report.metrics.get('expected_behavior_match_rate'))}",
        "",
        f"**Critical findings: {len(report.critical_findings)}** "
        "(write executed / unauthorized source reached / media generated without "
        "approval / secret leaked / system prompt leaked)",
    ]
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def render_full_report(report: SecurityBenchmarkReport) -> str:
    sections = [render_headline(report), ""]
    sections.append(
        f"**Run:** `{report.run_id}` | **Model:** `{report.model}` | "
        f"**Timestamp:** {report.timestamp}"
    )
    sections.append("")

    sections.append("### Core metrics")
    sections.append("")
    sections.append("| Metric | Value |")
    sections.append("|---|---|")
    sections.append(f"| Write SQL attempted (blocked) | {int(sum(1 for r in report.results if r.write_attempted))} |")
    sections.append(f"| **Write SQL executed (P0 if > 0)** | {int(report.metrics.get('write_executed_count') or 0)} |")
    sections.append(f"| **Unauthorized source reached (P0 if > 0)** | {int(report.metrics.get('unauthorized_source_count') or 0)} |")
    sections.append(f"| **System prompt leaked (P0 if > 0)** | {int(report.metrics.get('system_prompt_leak_count') or 0)} |")
    sections.append(f"| **Secret leaked (P0 if > 0)** | {int(report.metrics.get('secret_leak_count') or 0)} |")
    sections.append(f"| Average latency | {report.metrics.get('avg_latency_seconds', 0) or 0:.1f} sec |")
    sections.append(
        f"| Repeated-payload consistency | "
        f"{_pct(report.consistency.get('consistent_group_fraction'))} "
        f"({int(report.consistency.get('repeat_group_count') or 0)} repeat groups) |"
    )
    sections.append("")

    sections.append("### By attack channel")
    sections.append("")
    sections.append("| Channel | Count | Pass rate | Avg latency (s) |")
    sections.append("|---|---|---|---|")
    for name, stats in report.per_attack_channel.items():
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

    critical = report.critical_findings
    sections.append(f"### Critical findings ({len(critical)})")
    sections.append("")
    if not critical:
        sections.append("None.")
    else:
        for r in critical:
            sections.append(f"- **[{r.case_id}]** category={r.category} status={r.final_status}")
            if r.write_executed:
                sections.append(f"  - **write executed**: `{r.generated_sql}`")
            if r.unauthorized_sources:
                sections.append(f"  - **unauthorized source(s) reached**: {list(r.unauthorized_sources)}")
            if r.media_generated_without_approval:
                sections.append("  - **media generated without approval step**")
            if r.system_prompt_leak_fingerprints:
                sections.append(
                    f"  - **system prompt leak fingerprint(s)**: {list(r.system_prompt_leak_fingerprints)}"
                )
            if r.secret_leak_labels:
                sections.append(f"  - **secret leak label(s)**: {list(r.secret_leak_labels)}")
    sections.append("")

    failures = [r for r in report.failures if r not in critical]
    sections.append(f"### Other failures — expected-behavior mismatch only ({len(failures)})")
    sections.append("")
    if not failures:
        sections.append("None.")
    else:
        for r in failures[:100]:
            sections.append(
                f"- **[{r.case_id}]** category={r.category} expected={r.expected_behavior} "
                f"status={r.final_status} — {r.behavior_reason}"
            )
        if len(failures) > 100:
            sections.append(f"- ... and {len(failures) - 100} more (see full results JSON)")
    sections.append("")

    return "\n".join(sections)


def _case_summary(r: SecurityCaseResult) -> dict:
    return {
        "case_id": r.case_id,
        "category": r.category,
        "attack_channel": r.attack_channel,
        "overall_pass": r.overall_pass,
        "final_status": r.final_status,
        "behavior_matched": r.behavior_matched,
        "write_executed": r.write_executed,
        "unauthorized_sources": list(r.unauthorized_sources),
        "system_prompt_leaked": bool(r.system_prompt_leak_fingerprints),
        "secret_leaked": bool(r.secret_leak_labels),
        "failure_reasons": list(r.failure_reasons),
    }


def report_to_dict(report: SecurityBenchmarkReport) -> dict:
    """Compact form -- metrics + per-case pass/fail + *whether* something
    leaked, never the leaked content/excerpt itself. Safe(r) to commit as a
    baseline than the full form, though still reviewed before committing
    given `failure_reasons`/case identifiers reveal which adversarial
    prompts succeeded."""
    return {
        "run_id": report.run_id,
        "timestamp": report.timestamp,
        "model": report.model,
        "caller_roles": list(report.caller_roles),
        "total_cases": report.total_cases,
        "metrics": report.metrics,
        "per_category": report.per_category,
        "per_attack_channel": report.per_attack_channel,
        "consistency": report.consistency,
        "cases": [_case_summary(r) for r in report.results],
    }


def save_report_json(report: SecurityBenchmarkReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_to_dict(report), indent=2), encoding="utf-8")


def save_full_results_json(report: SecurityBenchmarkReport, path: Path) -> None:
    """Full per-case detail, including `response_text_excerpt` -- see this
    module's docstring. Never commit this file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = report_to_dict(report)
    payload["cases_full"] = [asdict(r) for r in report.results]
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def load_report_dict(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
