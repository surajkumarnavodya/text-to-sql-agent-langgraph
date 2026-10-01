"""Standalone entry point: runs the 500-case prompt-injection security
benchmark (`eval/security_benchmark/cases/prompt_injection_benchmark_500.csv`)
against the real, live agent and prints/saves a full report.

Requires a live DB connection + Ollama, like `scripts/run_benchmark.py` --
not part of the `pytest` suite, never run by CI (each case is a real LLM
round trip through the full orchestrated pipeline).

Usage (from repo root, with the venv activated):

    python scripts\\run_security_benchmark.py                       # full 500 cases, role=viewer
    python scripts\\run_security_benchmark.py --limit 20             # first 20 cases
    python scripts\\run_security_benchmark.py --category "Direct instruction override" "SQL safety bypass"
    python scripts\\run_security_benchmark.py --roles admin          # re-run under a privileged identity
    python scripts\\run_security_benchmark.py --save-baseline        # record this run as the new baseline
    python scripts\\run_security_benchmark.py --check-regression     # compare against eval/security_baselines/latest.json

See `eval/security_benchmark/runner.py`'s module docstring for why
`--roles viewer` is the default rather than `admin`, and
`eval/security_benchmark/dataset.py`'s module docstring for what the
dataset's own 500-vs-200-unique-payload structure means for run time and
interpretation.
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Adversarial cases in this benchmark deliberately contain instruction-
# override/jailbreak-shaped text; none are known to contain non-ASCII
# content today, but mirroring scripts/run_benchmark.py's own guard costs
# nothing and avoids the identical failure mode if a future case does.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config.settings import configure_logging, get_settings  # noqa: E402
from eval.security_benchmark.dataset import load_security_cases  # noqa: E402
from eval.security_benchmark.reporting import (  # noqa: E402
    load_report_dict,
    render_full_report,
    save_full_results_json,
    save_report_json,
)
from eval.security_benchmark.runner import build_report, run_security_benchmark  # noqa: E402
from eval.security_benchmark.schema import SecurityCaseResult  # noqa: E402

_RESULTS_DIR = Path(__file__).resolve().parent.parent / "eval" / "security_results"
_BASELINE_PATH = (
    Path(__file__).resolve().parent.parent / "eval" / "security_baselines" / "latest.json"
)


def _print_progress(index: int, total: int, result: SecurityCaseResult) -> None:
    outcome = "PASS" if result.overall_pass else "FAIL"
    marker = (
        "!!!"
        if (
            result.write_executed
            or result.unauthorized_sources
            or result.secret_leak_labels
            or result.system_prompt_leak_fingerprints
        )
        else "   "
    )
    print(
        f"{marker}[{outcome}] ({index}/{total}) {result.case_id} [{result.category}] "
        f"status={result.final_status} time={result.wall_time_seconds:.1f}s",
        flush=True,
    )
    if outcome == "FAIL":
        print(f"         {'; '.join(result.failure_reasons)}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=None, help="Run at most this many cases.")
    parser.add_argument("--category", nargs="*", default=None, help="Only run these categories.")
    parser.add_argument(
        "--roles",
        nargs="*",
        default=["viewer"],
        help="Caller role(s) to run every case as (default: viewer -- see runner.py's docstring).",
    )
    parser.add_argument(
        "--save-baseline", action="store_true", help="Save this run as the new regression baseline."
    )
    parser.add_argument(
        "--check-regression",
        action="store_true",
        help="Compare against the stored baseline; exit 1 if a new critical finding appears.",
    )
    parser.add_argument("--log-level", default="WARNING", help="Root log level (default WARNING).")
    args = parser.parse_args()

    configure_logging(args.log_level)
    settings = get_settings()

    cases = load_security_cases()
    print(f"Loaded {len(cases)} case(s).")
    categories = set(args.category) if args.category else None
    caller_roles = tuple(args.roles)

    results = run_security_benchmark(
        cases,
        caller_roles=caller_roles,
        limit=args.limit,
        categories=categories,
        progress_callback=_print_progress,
    )

    run_id = datetime.now(UTC).strftime("secrun_%Y%m%dT%H%M%SZ")
    timestamp = datetime.now(UTC).isoformat()
    report = build_report(run_id, timestamp, settings.ollama_model, caller_roles, cases, results)

    print()
    print(render_full_report(report))

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    compact_path = _RESULTS_DIR / f"{run_id}.json"
    full_path = _RESULTS_DIR / f"{run_id}_full.json"
    save_report_json(report, compact_path)
    save_full_results_json(report, full_path)
    print(f"\nSaved compact results to {compact_path}")
    print(f"Saved full results (incl. leaked-content excerpts, if any) to {full_path}")
    print(
        "NOTE: the _full.json file may contain real leaked content on a failing case -- do not commit it."
    )

    exit_code = 1 if report.critical_findings else 0

    if args.check_regression:
        if not _BASELINE_PATH.exists():
            print(f"\nNo baseline found at {_BASELINE_PATH} -- skipping regression check.")
        else:
            baseline = load_report_dict(_BASELINE_PATH)
            baseline_critical = {
                c["case_id"]
                for c in baseline.get("cases", [])
                if c.get("write_executed")
                or c.get("unauthorized_sources")
                or c.get("system_prompt_leaked")
                or c.get("secret_leaked")
            }
            current_critical = {r.case_id for r in report.critical_findings}
            new_critical = current_critical - baseline_critical
            print(f"\n--- Regression check against {_BASELINE_PATH.name} ---")
            if new_critical:
                print(f"NEW critical finding(s) not present in baseline: {sorted(new_critical)}")
                exit_code = 1
            else:
                print("No new critical findings vs. baseline.")

    if args.save_baseline:
        save_report_json(report, _BASELINE_PATH)
        print(f"\nSaved this run as the new baseline: {_BASELINE_PATH}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
