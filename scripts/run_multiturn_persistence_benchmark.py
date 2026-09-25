"""Standalone entry point: runs the true multi-turn persistence test (see
`eval/security_benchmark/multiturn.py`'s module docstring) against the
real, live agent and prints/saves a report.

Requires a live DB connection + Ollama, like `scripts/run_security_benchmark.py`
-- not part of the `pytest` suite, never run by CI. Each of the 10 unique
"Multi-turn persistence" payloads costs 2 real LLM round trips (turn 1 +
turn 2), plus one shared control run -- 21 live calls total.

Usage (from repo root, with the venv activated):

    python scripts\\run_multiturn_persistence_benchmark.py
    python scripts\\run_multiturn_persistence_benchmark.py --roles admin
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config.settings import configure_logging  # noqa: E402
from eval.security_benchmark.multiturn import (  # noqa: E402
    MultiTurnPersistenceResult,
    render_report,
    run_multiturn_persistence_benchmark,
)

_RESULTS_DIR = Path(__file__).resolve().parent.parent / "eval" / "security_results"


def _print_progress(index: int, total: int, result: MultiTurnPersistenceResult) -> None:
    marker = "!!!" if result.critical_finding else "   "
    print(
        f"{marker}({index}/{total}) turn1_status={result.turn1_status} "
        f"became_history={result.turn1_became_history} "
        f"turn2_held={result.poisoned_turn2.held} :: {result.payload[:70]!r}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--roles",
        nargs="*",
        default=["viewer"],
        help="Caller role(s) for every turn (default: viewer).",
    )
    parser.add_argument("--log-level", default="WARNING", help="Root log level (default WARNING).")
    args = parser.parse_args()

    configure_logging(args.log_level)
    caller_roles = tuple(args.roles)

    print("Running control turn 2 (no history) ...")
    control, results = run_multiturn_persistence_benchmark(
        caller_roles=caller_roles, progress_callback=_print_progress
    )

    report = render_report(control, results)
    print()
    print(report)

    run_id = datetime.now(UTC).strftime("multiturn_%Y%m%dT%H%M%SZ")
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _RESULTS_DIR / f"{run_id}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nSaved report to {report_path}")

    critical = [r for r in results if r.critical_finding]
    return 1 if critical else 0


if __name__ == "__main__":
    sys.exit(main())
