"""Renders a `MediaBenchmarkReport` as a short markdown summary -- mirrors
`eval/reporting.py`'s role, scaled to this benchmark's much smaller metric
set.
"""

from __future__ import annotations

from eval.media_benchmark.schema import MediaBenchmarkReport


def render_report(report: MediaBenchmarkReport) -> str:
    lines = [
        f"# Media Search Benchmark -- run {report.run_id}",
        f"_{report.timestamp}_",
        "",
        f"- Total cases: {report.total_cases}",
        f"- Hit@1: {report.hit_at_1:.0%}",
        f"- Hit@k: {report.hit_at_k:.0%}",
    ]
    if report.timestamp_accuracy is not None:
        lines.append(f"- Timestamp accuracy: {report.timestamp_accuracy:.0%}")

    if report.failures:
        lines.append("")
        lines.append("## Failures")
        for result in report.failures:
            detail = (
                result.error
                or f"expected {result.expected_filename!r}, got {result.hit_filenames!r}"
            )
            lines.append(f"- `{result.case_id}` ({result.query!r}): {detail}")

    return "\n".join(lines)
