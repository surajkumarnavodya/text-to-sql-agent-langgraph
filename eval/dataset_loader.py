"""Loads and validates the benchmark dataset from `eval/benchmark/*.yaml`.

Split across multiple files by difficulty/category (`easy.yaml`,
`medium.yaml`, `hard.yaml`, `real_world.yaml`, `adversarial.yaml`,
`follow_up.yaml`, `cost_estimation.yaml`) purely for human maintainability
-- this loader merges every `*.yaml` file in the directory into one
`BenchmarkDataset`, so splitting or consolidating files never requires a
code change, and adding a new file is enough to add a new category.

Pure and dependency-free (no live DB, no Ollama) -- this is what makes it
safe to unit test directly (see `tests/test_eval_dataset_loader.py`) and
safe to call from `eval.runner` without any live-infrastructure precondition.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from eval.schema import (
    KNOWN_CATEGORIES,
    BenchmarkCase,
    BenchmarkDataset,
    ExpectedResultSpec,
    FollowUpCase,
    FollowUpTurn,
)

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).resolve().parent / "benchmark"

_REQUIRED_CASE_FIELDS = ("id", "question", "database", "difficulty", "category")
_REQUIRED_FOLLOWUP_FIELDS = ("id", "database", "turns")


class DatasetValidationError(ValueError):
    """Raised when a benchmark YAML file is malformed in a way that would
    silently corrupt evaluation results if allowed through (a missing
    required field, a duplicate case id) -- fail fast, the same philosophy
    `config/settings.py` already applies to malformed `.env` values, rather
    than skip a broken entry and quietly evaluate fewer cases than intended.
    """


def _parse_expected_result(raw: dict | None) -> ExpectedResultSpec | None:
    if raw is None:
        return None
    sample_rows = raw.get("sample_rows")
    return ExpectedResultSpec(
        row_count=raw.get("row_count"),
        columns=tuple(raw["columns"]) if raw.get("columns") is not None else None,
        sample_rows=tuple(tuple(row) for row in sample_rows) if sample_rows is not None else None,
    )


def _parse_case(raw: dict, source_file: Path) -> BenchmarkCase:
    missing = [f for f in _REQUIRED_CASE_FIELDS if not raw.get(f)]
    if missing:
        raise DatasetValidationError(
            f"{source_file.name}: case missing required field(s) {missing}: {raw!r}"
        )
    category = raw["category"]
    if category not in KNOWN_CATEGORIES:
        logger.warning(
            "[dataset_loader] %s: case %r uses an unrecognized category %r "
            "(not in eval.schema.KNOWN_CATEGORIES -- still loaded, just won't "
            "be grouped under a standard category in the report)",
            source_file.name,
            raw["id"],
            category,
        )
    return BenchmarkCase(
        id=raw["id"],
        question=raw["question"],
        database=raw["database"],
        difficulty=raw["difficulty"],
        category=category,
        security_classification=raw.get("security_classification", "benign"),
        expected_tables=tuple(raw.get("expected_tables") or ()),
        expected_columns=tuple(raw.get("expected_columns") or ()),
        expected_sql=raw.get("expected_sql"),
        alternative_sql=tuple(raw.get("alternative_sql") or ()),
        expected_result=_parse_expected_result(raw.get("expected_result")),
        expected_behavior=raw.get("expected_behavior", "succeed"),
        expect_rejection_reason=raw.get("expect_rejection_reason"),
        expect_cost_severity=raw.get("expect_cost_severity"),
        order_matters=bool(raw.get("order_matters", False)),
        min_rows=raw.get("min_rows"),
        max_rows=raw.get("max_rows"),
        expect_readable_result=bool(raw.get("expect_readable_result", False)),
        expect_grounded_insight=bool(raw.get("expect_grounded_insight", False)),
        notes=raw.get("notes", ""),
    )


def _parse_turn(raw: dict) -> FollowUpTurn:
    if not raw.get("question"):
        raise DatasetValidationError(f"follow-up turn missing 'question': {raw!r}")
    return FollowUpTurn(
        question=raw["question"],
        expected_tables=tuple(raw.get("expected_tables") or ()),
        expected_columns=tuple(raw.get("expected_columns") or ()),
        expected_sql=raw.get("expected_sql"),
        alternative_sql=tuple(raw.get("alternative_sql") or ()),
        expected_result=_parse_expected_result(raw.get("expected_result")),
        expected_behavior=raw.get("expected_behavior", "succeed"),
        expect_followup=bool(raw.get("expect_followup", False)),
        order_matters=bool(raw.get("order_matters", False)),
        min_rows=raw.get("min_rows"),
        max_rows=raw.get("max_rows"),
        expect_readable_result=bool(raw.get("expect_readable_result", False)),
        expect_grounded_insight=bool(raw.get("expect_grounded_insight", False)),
    )


def _parse_followup_case(raw: dict, source_file: Path) -> FollowUpCase:
    missing = [f for f in _REQUIRED_FOLLOWUP_FIELDS if not raw.get(f)]
    if missing:
        raise DatasetValidationError(
            f"{source_file.name}: follow-up case missing required field(s) {missing}: {raw!r}"
        )
    turns = tuple(_parse_turn(t) for t in raw["turns"])
    if len(turns) < 2:
        raise DatasetValidationError(
            f"{source_file.name}: follow-up case {raw['id']!r} has fewer than 2 turns "
            "-- a single-turn 'follow-up' isn't a follow-up at all; use a standalone case."
        )
    return FollowUpCase(
        id=raw["id"],
        database=raw["database"],
        turns=turns,
        difficulty=raw.get("difficulty", "real_world"),
        category=raw.get("category", "follow_up"),
        notes=raw.get("notes", ""),
    )


def load_benchmark(directory: Path | None = None) -> BenchmarkDataset:
    """Loads every `*.yaml` file in `directory` into one `BenchmarkDataset`.

    Args:
        directory: Override for the benchmark directory (mainly for tests).
            Defaults to `eval/benchmark/`.

    Returns:
        A `BenchmarkDataset` with every standalone and follow-up case from
        every file, in file-then-declaration order (deterministic, so a
        report's case ordering is stable across runs of the same dataset).

    Raises:
        DatasetValidationError: a case is missing a required field, a
            follow-up case has fewer than 2 turns, or a case id is
            duplicated (across files or within one) -- silently allowing a
            duplicate id would corrupt regression-baseline lookups, which
            key results by case id.
    """
    resolved_dir = directory or _DEFAULT_DIR
    files = sorted(resolved_dir.glob("*.yaml"))

    standalone: list[BenchmarkCase] = []
    followups: list[FollowUpCase] = []
    seen_ids: dict[str, Path] = {}

    for path in files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for raw_case in raw.get("cases") or []:
            case = _parse_case(raw_case, path)
            if case.id in seen_ids:
                raise DatasetValidationError(
                    f"Duplicate case id {case.id!r} in {path.name} "
                    f"(first seen in {seen_ids[case.id].name})"
                )
            seen_ids[case.id] = path
            standalone.append(case)
        for raw_case in raw.get("followup_cases") or []:
            followup_case = _parse_followup_case(raw_case, path)
            if followup_case.id in seen_ids:
                raise DatasetValidationError(
                    f"Duplicate case id {followup_case.id!r} in {path.name} "
                    f"(first seen in {seen_ids[followup_case.id].name})"
                )
            seen_ids[followup_case.id] = path
            followups.append(followup_case)

    logger.info(
        "[dataset_loader] loaded %d standalone case(s) and %d follow-up sequence(s) "
        "from %d file(s) in %s",
        len(standalone),
        len(followups),
        len(files),
        resolved_dir,
    )
    return BenchmarkDataset(standalone_cases=tuple(standalone), followup_cases=tuple(followups))
