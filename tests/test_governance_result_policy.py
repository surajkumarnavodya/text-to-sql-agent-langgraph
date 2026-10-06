"""Result-level governance: masking restricted columns and redacting secret-shaped cells."""

from __future__ import annotations

import pytest
from governance.result_policy import RESTRICTED_MASK, govern_result, govern_rows_for_caller

COLUMNS = ["department", "employee_name", "salary"]
ROWS = [
    ("Engineering", "Asha Rao", 120000),
    ("Sales", "Ben Ortiz", 95000),
]


def test_restricted_column_is_masked_for_a_caller_without_permission():
    result = govern_result(COLUMNS, ROWS, {"salary"}, can_view_restricted=False)
    assert [row[2] for row in result.rows] == [RESTRICTED_MASK, RESTRICTED_MASK]
    assert [row[0] for row in result.rows] == ["Engineering", "Sales"]
    assert result.actions == ("masked_restricted_columns:salary",)


def test_restricted_column_is_visible_to_a_caller_with_permission():
    result = govern_result(COLUMNS, ROWS, {"salary"}, can_view_restricted=True)
    assert result.rows == [("Engineering", "Asha Rao", 120000), ("Sales", "Ben Ortiz", 95000)]
    assert result.actions == ()


def test_column_name_matching_is_case_insensitive():
    result = govern_result(["Salary"], [(120000,)], {"salary"}, can_view_restricted=False)
    assert result.rows == [(RESTRICTED_MASK,)]


def test_null_in_a_masked_column_is_masked_too():
    result = govern_result(["salary"], [(None,)], {"salary"}, can_view_restricted=False)
    assert result.rows == [(RESTRICTED_MASK,)]


def test_row_shape_is_preserved_for_tuples_and_lists():
    as_list = govern_result(["a"], [["x"]], set(), can_view_restricted=False)
    as_tuple = govern_result(["a"], [("x",)], set(), can_view_restricted=False)
    assert as_list.rows == [["x"]]
    assert isinstance(as_list.rows[0], list)
    assert as_tuple.rows == [("x",)]
    assert isinstance(as_tuple.rows[0], tuple)


def test_input_rows_are_never_mutated():
    rows = [("Engineering", "Asha Rao", 120000)]
    govern_result(COLUMNS, rows, {"salary"}, can_view_restricted=False)
    assert rows == [("Engineering", "Asha Rao", 120000)]


def test_ordinary_data_passes_through_unchanged_with_no_actions():
    result = govern_result(
        ["department", "headcount"], [("Sales", 14)], set(), can_view_restricted=False
    )
    assert result.rows == [("Sales", 14)]
    assert result.actions == ()
    assert not result.changed


@pytest.mark.parametrize(
    "secret_value",
    [
        "postgresql://app:hunter2@db.internal/prod",
        "Server=db;Database=hr;User Id=sa;password=hunter2",
    ],
)
def test_secret_shaped_text_in_a_cell_is_redacted(secret_value):
    result = govern_result(["note"], [(secret_value,)], set(), can_view_restricted=True)
    assert "hunter2" not in result.rows[0][0]
    assert result.actions == ("redacted_secret_cells:1",)


def test_secret_redaction_counts_cells_without_recording_values():
    rows = [("password=one",), ("password=two",), ("ok",)]
    result = govern_result(["note"], rows, set(), can_view_restricted=True)
    assert result.actions == ("redacted_secret_cells:2",)
    assert all("one" not in action and "two" not in action for action in result.actions)


def test_numeric_and_null_cells_are_never_treated_as_secrets():
    result = govern_result(["n", "x"], [(42, None)], set(), can_view_restricted=True)
    assert result.rows == [(42, None)]
    assert result.actions == ()


def test_live_entry_point_masks_a_classified_column_by_caller_roles(monkeypatch):
    monkeypatch.setattr(
        "governance.result_policy.load_sensitive_columns",
        lambda: {("hr_employee", "salary"): "restricted", ("hr_employee", "dept"): "internal"},
    )
    rows = [("Sales", 95000)]
    columns = ["dept", "salary"]
    assert govern_rows_for_caller(columns, rows, ["viewer"]) == [("Sales", RESTRICTED_MASK)]
    assert govern_rows_for_caller(columns, rows, ["analyst"]) == [("Sales", 95000)]


def test_live_entry_point_leaves_unclassified_columns_alone_with_shipped_config():
    rows = [("Sales", 95000)]
    columns = ["department", "salary"]
    # The shipped config/sensitive_columns.yaml classifies nothing yet.
    assert govern_rows_for_caller(columns, rows, ["viewer"]) == [("Sales", 95000)]


def test_live_entry_point_returns_empty_rows_unchanged():
    assert govern_rows_for_caller(["a"], [], ["viewer"]) == []
