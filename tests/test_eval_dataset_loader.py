"""Unit tests for eval/dataset_loader.py. Fully offline -- writes temporary
YAML files, no real benchmark data or live DB/Ollama involved."""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.dataset_loader import DatasetValidationError, load_benchmark


def _write(tmp_path: Path, name: str, content: str) -> None:
    (tmp_path / name).write_text(content, encoding="utf-8")


class TestLoadBenchmark:
    def test_empty_directory_returns_empty_dataset(self, tmp_path: Path):
        dataset = load_benchmark(tmp_path)
        assert dataset.standalone_cases == ()
        assert dataset.followup_cases == ()

    def test_loads_a_valid_standalone_case(self, tmp_path: Path):
        _write(
            tmp_path,
            "a.yaml",
            """
            cases:
              - id: c1
                question: "How many customers?"
                database: TestDB
                difficulty: easy
                category: aggregation
                expected_sql: "SELECT COUNT(*) FROM Customers"
            """,
        )
        dataset = load_benchmark(tmp_path)
        assert len(dataset.standalone_cases) == 1
        case = dataset.standalone_cases[0]
        assert case.id == "c1"
        assert case.expected_sql == "SELECT COUNT(*) FROM Customers"
        assert case.security_classification == "benign"  # default

    def test_merges_multiple_files(self, tmp_path: Path):
        _write(
            tmp_path,
            "a.yaml",
            "cases:\n  - {id: c1, question: q1, database: d, difficulty: easy, category: sorting}\n",
        )
        _write(
            tmp_path,
            "b.yaml",
            "cases:\n  - {id: c2, question: q2, database: d, difficulty: medium, category: joins}\n",
        )
        dataset = load_benchmark(tmp_path)
        assert {c.id for c in dataset.standalone_cases} == {"c1", "c2"}

    def test_missing_required_field_raises(self, tmp_path: Path):
        _write(
            tmp_path,
            "a.yaml",
            "cases:\n  - {id: c1, database: d, difficulty: easy, category: sorting}\n",  # no question
        )
        with pytest.raises(DatasetValidationError, match="question"):
            load_benchmark(tmp_path)

    def test_duplicate_id_across_files_raises(self, tmp_path: Path):
        _write(
            tmp_path,
            "a.yaml",
            "cases:\n  - {id: dup, question: q1, database: d, difficulty: easy, category: sorting}\n",
        )
        _write(
            tmp_path,
            "b.yaml",
            "cases:\n  - {id: dup, question: q2, database: d, difficulty: easy, category: sorting}\n",
        )
        with pytest.raises(DatasetValidationError, match="Duplicate case id"):
            load_benchmark(tmp_path)

    def test_unrecognized_category_warns_but_still_loads(self, tmp_path: Path, caplog):
        _write(
            tmp_path,
            "a.yaml",
            "cases:\n  - {id: c1, question: q1, database: d, difficulty: easy, category: made_up_category}\n",
        )
        dataset = load_benchmark(tmp_path)
        assert len(dataset.standalone_cases) == 1

    def test_expected_result_spec_is_parsed(self, tmp_path: Path):
        _write(
            tmp_path,
            "a.yaml",
            """
            cases:
              - id: c1
                question: q1
                database: d
                difficulty: easy
                category: aggregation
                expected_result:
                  row_count: 1
                  columns: [cnt]
                  sample_rows: [[42]]
            """,
        )
        dataset = load_benchmark(tmp_path)
        spec = dataset.standalone_cases[0].expected_result
        assert spec is not None
        assert spec.row_count == 1
        assert spec.columns == ("cnt",)
        assert spec.sample_rows == ((42,),)

    def test_alternative_sql_parsed_as_tuple(self, tmp_path: Path):
        _write(
            tmp_path,
            "a.yaml",
            """
            cases:
              - id: c1
                question: q1
                database: d
                difficulty: easy
                category: aggregation
                expected_sql: "SELECT 1"
                alternative_sql:
                  - "SELECT 1 AS x"
                  - "SELECT 1 AS y"
            """,
        )
        dataset = load_benchmark(tmp_path)
        assert dataset.standalone_cases[0].alternative_sql == ("SELECT 1 AS x", "SELECT 1 AS y")


class TestLoadFollowupCases:
    def test_loads_a_valid_followup_case(self, tmp_path: Path):
        _write(
            tmp_path,
            "f.yaml",
            """
            followup_cases:
              - id: f1
                database: d
                turns:
                  - question: "Show sales by year"
                    min_rows: 1
                  - question: "Now by quarter"
                    expect_followup: true
            """,
        )
        dataset = load_benchmark(tmp_path)
        assert len(dataset.followup_cases) == 1
        case = dataset.followup_cases[0]
        assert len(case.turns) == 2
        assert case.turns[1].expect_followup is True
        assert case.difficulty == "real_world"  # default
        assert case.category == "follow_up"  # default

    def test_single_turn_followup_raises(self, tmp_path: Path):
        _write(
            tmp_path,
            "f.yaml",
            """
            followup_cases:
              - id: f1
                database: d
                turns:
                  - question: "Only one turn"
            """,
        )
        with pytest.raises(DatasetValidationError, match="fewer than 2 turns"):
            load_benchmark(tmp_path)

    def test_missing_required_field_raises(self, tmp_path: Path):
        _write(
            tmp_path,
            "f.yaml",
            "followup_cases:\n  - {database: d, turns: [{question: q1}, {question: q2}]}\n",  # no id
        )
        with pytest.raises(DatasetValidationError, match="id"):
            load_benchmark(tmp_path)


class TestRealDatasetIntegrity:
    """Loads the *real* eval/benchmark/ dataset (no mocking) -- a structural
    regression guard so a future hand-edit to the YAML files (a typo'd
    field, a duplicate id, a single-turn "follow-up") is caught by `pytest`
    immediately, without needing a live run to discover it."""

    def test_real_dataset_loads_without_error(self):
        dataset = load_benchmark()
        assert len(dataset.standalone_cases) > 0
        assert len(dataset.followup_cases) > 0

    def test_real_dataset_has_no_duplicate_ids(self):
        dataset = load_benchmark()
        ids = [c.id for c in dataset.standalone_cases] + [c.id for c in dataset.followup_cases]
        assert len(ids) == len(set(ids))

    def test_every_adversarial_case_has_a_non_succeed_behavior(self):
        dataset = load_benchmark()
        for case in dataset.standalone_cases:
            if case.security_classification == "adversarial":
                assert case.expected_behavior != "succeed", (
                    f"{case.id} is classified adversarial but expects 'succeed' -- "
                    "likely a copy-paste mistake"
                )

    def test_every_succeed_case_has_some_grading_signal(self):
        """A "succeed" case with no expected_sql, no expected_result, and no
        min_rows/expected_tables would silently always pass -- every
        standalone accuracy case must have at least one real check."""
        dataset = load_benchmark()
        for case in dataset.standalone_cases:
            if case.expected_behavior != "succeed":
                continue
            has_signal = bool(
                case.expected_sql
                or case.expected_result
                or case.min_rows is not None
                or case.expected_tables
                or case.expect_cost_severity
            )
            assert has_signal, f"{case.id} has no grading signal at all"
