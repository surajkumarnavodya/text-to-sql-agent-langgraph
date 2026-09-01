"""Standalone entry point: prints a plain-text operational summary for the
maintainer's periodic review, per docs/GOVERNANCE.md's "Review cadence"
("Review scripts/monitoring_summary.py's output weekly during active
development... outside active development, whenever you next sit down to
work on this").

Two independent sections, each degrading gracefully if its input isn't
available rather than failing the whole script:

1. Benchmark trend -- reads eval/baselines/latest.json (the accuracy/
   retrieval/latency/security numbers from the most recent benchmark run
   saved as a baseline via `scripts/run_benchmark.py --save-baseline`).
2. Security-event summary -- this app logs everywhere via the standard
   `logging` module to stdout/stderr (config/settings.py::configure_logging),
   with no file handler configured by default, so there's no standing log
   file to read unless the operator redirects output to one themselves
   (e.g. `python scripts/run_benchmark.py > run.log 2>&1`, or a container's
   captured stdout). Pass --log-file to summarize security.audit events
   (agent_rejected, sql_safety_violation, rate_limit_tripped, ...) from
   such a file; without it, this section reports itself as unavailable
   rather than silently omitting information the reader might expect.

Usage:

    python scripts\\monitoring_summary.py
    python scripts\\monitoring_summary.py --log-file eval\\results\\live_run_output.log
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BASELINE_PATH = Path(__file__).resolve().parent.parent / "eval" / "baselines" / "latest.json"

# Matches security/audit_log.py::log_security_event's rendered line shape:
# "event=<type> severity=<level> detail='...' [correlation_id=... ...]"
_AUDIT_EVENT_RE = re.compile(r"event=(?P<event_type>\S+) severity=(?P<severity>\S+)")

_KEY_METRICS = (
    "final_accuracy",
    "result_set_accuracy",
    "sql_execution_accuracy",
    "schema_retrieval_recall",
    "security_rejection_accuracy",
    "average_latency_seconds",
    "p95_latency_seconds",
)


def _print_header(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


def _summarize_baseline(path: Path) -> None:
    _print_header("Benchmark baseline")
    if not path.exists():
        print(f"No baseline found at {path}.")
        print("Run: python scripts\\run_benchmark.py --save-baseline")
        return

    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"Run:      {data.get('run_id')} ({data.get('timestamp')})")
    print(f"Model:    {data.get('model')}")
    print(f"Database: {data.get('database')}")
    print(f"Cases:    {data.get('total_cases')}")
    print()

    metrics = data.get("metrics", {})
    for name in _KEY_METRICS:
        value = metrics.get(name)
        if value is None:
            print(f"  {name:32s} not measured")
        elif "latency" in name:
            print(f"  {name:32s} {value:.1f}s")
        else:
            print(f"  {name:32s} {value * 100:.1f}%")

    failures = [c for c in data.get("cases", []) if not c.get("overall_pass")]
    if failures:
        print(f"\n{len(failures)}/{data.get('total_cases')} case(s) failing in this baseline:")
        by_category: Counter[str] = Counter(
            c.get("failure_category") or "unknown" for c in failures
        )
        for category, count in by_category.most_common():
            print(f"  {category:24s} {count}")


def _summarize_log_file(path: Path | None) -> None:
    _print_header("Security events")
    if path is None:
        print("No --log-file given -- nothing to summarize.")
        print(
            "This app logs security.audit events to stdout/stderr with no file handler by "
            "default (config/settings.py::configure_logging). Redirect output to a file "
            "(e.g. `python scripts\\run_benchmark.py > run.log 2>&1`, or a container's "
            "captured logs) and pass it with --log-file to see a summary here."
        )
        return
    if not path.exists():
        print(f"--log-file {path} does not exist.")
        return

    counts: Counter[tuple[str, str]] = Counter()
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if "security.audit" not in line and "event=" not in line:
                continue
            match = _AUDIT_EVENT_RE.search(line)
            if match:
                counts[(match.group("event_type"), match.group("severity"))] += 1

    if not counts:
        print(f"No security.audit event lines found in {path}.")
        return

    print(f"From {path}:\n")
    for (event_type, severity), count in counts.most_common():
        print(f"  {count:5d}  {severity:8s} {event_type}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Path to a captured log file to summarize security.audit events from.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=_BASELINE_PATH,
        help=f"Path to the benchmark baseline JSON (default: {_BASELINE_PATH}).",
    )
    args = parser.parse_args()

    _summarize_baseline(args.baseline)
    _summarize_log_file(args.log_file)
    print()


if __name__ == "__main__":
    main()
