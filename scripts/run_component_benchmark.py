"""Standalone entry point: runs the component benchmark
(`eval/component_benchmark/`) and prints a pass/fail report.

Unlike `scripts/run_benchmark.py`/`scripts/run_security_benchmark.py`,
this needs no live database or LLM -- every case calls a real,
deterministic production function directly. It's also run automatically
on every `pytest` invocation (`tests/test_eval_component_benchmark.py`),
so this script exists for the same reason `scripts/run_benchmark.py`
does despite its own cases also being testable: a quick, readable,
human-facing report an operator can run on demand, with the baseline-
management flags pytest itself has no reason to expose.

Usage (from repo root, with the venv activated):

    python scripts\\run_component_benchmark.py                 # run and print a report
    python scripts\\run_component_benchmark.py --save-baseline  # record this run as the new baseline
    python scripts\\run_component_benchmark.py --check-regression  # compare against the committed baseline, exit 1 on regression
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.component_benchmark.regression import (  # noqa: E402
    compare_against_baseline,
    load_baseline,
    save_baseline,
)
from eval.component_benchmark.runner import run_all  # noqa: E402

_BASELINE_PATH = (
    Path(__file__).resolve().parent.parent
    / "eval"
    / "baselines"
    / "component_benchmark_latest.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--save-baseline", action="store_true", help="Record this run as the new baseline."
    )
    parser.add_argument(
        "--check-regression",
        action="store_true",
        help="Compare against the committed baseline; exit 1 on any regression.",
    )
    args = parser.parse_args()

    report = run_all()

    for domain in report.domains:
        print(f"\n=== {domain.domain} ({domain.cases_passed}/{domain.cases_run} passed) ===")
        for result in domain.results:
            marker = "PASS" if result.passed else "FAIL"
            print(f"  [{marker}] {result.name}: {result.description}")
            if not result.passed:
                print(f"         detail: {result.detail}")

    print(f"\nTotal: {report.total_passed}/{report.total_cases} passed")

    exit_code = 0
    if args.check_regression:
        baseline = load_baseline(_BASELINE_PATH)
        comparison = compare_against_baseline(report, baseline)
        if comparison.regressions:
            print("\nREGRESSIONS:")
            for reg in comparison.regressions:
                print(
                    f"  {reg.domain}/{reg.case_name}: was passing, now failing -- {reg.current_detail}"
                )
            exit_code = 1
        if comparison.new_cases:
            print(f"\nNew cases since baseline: {comparison.new_cases}")
        if comparison.removed_cases:
            print(f"\nRemoved cases since baseline: {comparison.removed_cases}")
        if not comparison.regressions:
            print("\nNo regressions against the committed baseline.")

    if not report.all_passed and not args.check_regression:
        exit_code = 1

    if args.save_baseline:
        save_baseline(report, _BASELINE_PATH)
        print(f"\nSaved baseline to {_BASELINE_PATH}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
