"""Standalone entry point: runs the Text-to-SQL benchmark (`eval/benchmark/*.yaml`)
against the real agent and prints/saves a full evaluation report.

Requires a live DB connection + Ollama, like `scripts/integration_test.py` --
not part of the `pytest` suite, never run by CI (each case is a real LLM
round trip; a full run can take a long time -- see `--limit`).

Usage (from repo root, with the venv activated):

    python scripts\\run_benchmark.py                        # full dataset
    python scripts\\run_benchmark.py --limit 20              # first 20 cases
    python scripts\\run_benchmark.py --difficulty easy medium
    python scripts\\run_benchmark.py --category joins ctes
    python scripts\\run_benchmark.py --save-baseline         # record this run as the new baseline
    python scripts\\run_benchmark.py --check-regression      # compare against eval/baselines/latest.json, exit 1 on regression

This is the successor to `scripts/run_eval.py` (kept, unmodified, for
anyone still using it directly -- see the deprecation note at the top of
that file and of `eval/eval_questions.yaml`); every case those two files
had was migrated into `eval/benchmark/*.yaml` (mainly `adversarial.yaml`,
`follow_up.yaml`, and `cost_estimation.yaml`), so no existing regression
coverage was dropped.
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Adversarial benchmark cases deliberately contain non-ASCII text (e.g. a
# Cyrillic homoglyph regression case -- see eval/benchmark/adversarial.yaml)
# that a question/generated-SQL string can also legitimately contain.
# stdout's default encoding on Windows is the system codepage (cp1252), not
# UTF-8, when output isn't attached to a real console (e.g. redirected to a
# log file for a long background run) -- without this, printing such a
# question crashes the entire run with an UnicodeEncodeError partway
# through, rather than completing and reporting real results.
# `.reconfigure()` is only declared on the concrete `TextIOWrapper` (not the
# `TextIO` protocol `sys.stdout` is typed as), and isn't guaranteed to exist
# if stdout/stderr have been replaced by something else (e.g. under a test
# runner's capture) -- guarded rather than assumed, consistent with this
# project's general fail-open-on-non-critical-path approach.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config.settings import configure_logging, get_settings  # noqa: E402
from eval.dataset_loader import load_benchmark  # noqa: E402
from eval.metrics import build_report  # noqa: E402
from eval.regression import detect_regression  # noqa: E402
from eval.reporting import (  # noqa: E402
    load_report_dict,
    render_full_report,
    save_full_results_json,
    save_report_json,
)
from eval.runner import run_benchmark  # noqa: E402
from eval.schema import CaseRunResult  # noqa: E402

_RESULTS_DIR = Path(__file__).resolve().parent.parent / "eval" / "results"
_BASELINE_PATH = Path(__file__).resolve().parent.parent / "eval" / "baselines" / "latest.json"


def _print_progress(index: int, total: int, result: CaseRunResult) -> None:
    outcome = "PASS" if result.overall_pass else "FAIL"
    turn = f" turn={result.turn_index + 1}" if result.turn_index is not None else ""
    print(
        f"[{outcome}] ({index}/{total}) {result.case_id}{turn} "
        f"[{result.difficulty}/{result.category}] {result.question!r} "
        f"-- status={result.final_status} rows={result.row_count} "
        f"time={result.wall_time_seconds:.1f}s",
        flush=True,
    )
    if outcome == "FAIL":
        print(
            f"         failure_category={result.failure_category} -- {result.error_detail}",
            flush=True,
        )
        if result.generated_sql:
            print(f"         sql={result.generated_sql!r}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Run at most this many cases/sequences."
    )
    parser.add_argument("--category", nargs="*", default=None, help="Only run these categories.")
    parser.add_argument(
        "--difficulty", nargs="*", default=None, help="Only run these difficulties."
    )
    parser.add_argument(
        "--save-baseline", action="store_true", help="Save this run as the new regression baseline."
    )
    parser.add_argument(
        "--check-regression",
        action="store_true",
        help="Compare against the stored baseline; exit 1 if a regression is detected.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Root log level during the run (default WARNING, quiet).",
    )
    args = parser.parse_args()

    configure_logging(args.log_level)
    settings = get_settings()

    dataset = load_benchmark()
    total_available = len(dataset.standalone_cases) + len(dataset.followup_cases)
    print(
        f"Loaded {len(dataset.standalone_cases)} standalone case(s) and "
        f"{len(dataset.followup_cases)} follow-up sequence(s) "
        f"({total_available} total unit(s))."
    )
    categories = set(args.category) if args.category else None
    difficulties = set(args.difficulty) if args.difficulty else None

    results = run_benchmark(
        dataset,
        limit=args.limit,
        categories=categories,
        difficulties=difficulties,
        progress_callback=_print_progress,
    )

    run_id = datetime.now(UTC).strftime("run_%Y%m%dT%H%M%SZ")
    timestamp = datetime.now(UTC).isoformat()
    report = build_report(
        run_id, timestamp, settings.ollama_model, settings.db_name or "(unknown)", results
    )

    print()
    print(render_full_report(report))

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    compact_path = _RESULTS_DIR / f"{run_id}.json"
    full_path = _RESULTS_DIR / f"{run_id}_full.json"
    save_report_json(report, compact_path)
    save_full_results_json(report, full_path)
    print(f"\nSaved compact results to {compact_path}")
    print(f"Saved full results (incl. generated SQL/rows) to {full_path}")

    exit_code = 0
    if args.check_regression:
        if not _BASELINE_PATH.exists():
            print(f"\nNo baseline found at {_BASELINE_PATH} -- skipping regression check.")
        else:
            baseline = load_report_dict(_BASELINE_PATH)
            regression = detect_regression(baseline, report)
            print(f"\n--- Regression check against {_BASELINE_PATH.name} ---")
            print(regression.summary())
            if regression.has_regression:
                exit_code = 1

    if args.save_baseline:
        save_report_json(report, _BASELINE_PATH)
        print(f"\nSaved this run as the new baseline: {_BASELINE_PATH}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
