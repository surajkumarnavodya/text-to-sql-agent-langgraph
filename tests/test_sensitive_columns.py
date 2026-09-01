"""Unit tests for config/sensitive_columns.py and its enforcement point in
db/value_sampling.py (item F: sensitive-data handling)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine.default import DefaultDialect

from config.sensitive_columns import SensitivityTier, is_restricted, load_sensitive_columns
from db.schema_introspection import ColumnInfo, TableSchemaInfo
from db.value_sampling import attach_sample_values


def _write_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "sensitive_columns.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadSensitiveColumns:
    def test_missing_file_returns_empty_dict(self, tmp_path: Path):
        assert load_sensitive_columns(tmp_path / "does_not_exist.yaml") == {}

    def test_empty_template_returns_empty_dict(self, tmp_path: Path):
        path = _write_yaml(tmp_path, "tables: []\n")
        assert load_sensitive_columns(path) == {}

    def test_loads_a_restricted_column(self, tmp_path: Path):
        path = _write_yaml(
            tmp_path,
            """
            tables:
              - table_name: DimCustomer
                columns:
                  - column: EmailAddress
                    tier: restricted
            """,
        )
        result = load_sensitive_columns(path)
        assert result[("DimCustomer", "EmailAddress")] == "restricted"

    def test_unrecognized_tier_is_skipped_not_raised(self, tmp_path: Path):
        """A typo in the YAML (e.g. 'secret' instead of 'restricted') must
        not crash the whole load -- best-effort, same as
        config.table_descriptions.load_table_descriptions."""
        path = _write_yaml(
            tmp_path,
            """
            tables:
              - table_name: DimCustomer
                columns:
                  - column: EmailAddress
                    tier: secret
            """,
        )
        assert load_sensitive_columns(path) == {}

    def test_entry_missing_column_name_is_skipped(self, tmp_path: Path):
        path = _write_yaml(
            tmp_path,
            """
            tables:
              - table_name: DimCustomer
                columns:
                  - tier: restricted
            """,
        )
        assert load_sensitive_columns(path) == {}


class TestIsRestricted:
    def test_classified_restricted_column_is_restricted(self):
        classifications: dict[tuple[str, str], SensitivityTier] = {
            ("DimCustomer", "EmailAddress"): "restricted"
        }
        assert is_restricted("DimCustomer", "EmailAddress", classifications) is True

    def test_unclassified_column_defaults_to_not_restricted(self):
        assert is_restricted("DimCustomer", "SomeOtherColumn", {}) is False

    def test_internal_tier_is_not_restricted(self):
        """ "internal" is a real, distinct tier -- it must not be treated the
        same as "restricted" by this check (see docs/GOVERNANCE.md's tier
        definitions)."""
        classifications: dict[tuple[str, str], SensitivityTier] = {
            ("DimReseller", "BusinessName"): "internal"
        }
        assert is_restricted("DimReseller", "BusinessName", classifications) is False


class _CapturingCursorResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchmany(self, n: int) -> list[tuple]:
        return self._rows[:n]


class _StubConnection:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, _sql):
        return _CapturingCursorResult(self._rows)


class _StubEngine:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.dialect = DefaultDialect()

    def connect(self):
        return _StubConnection(self._rows)


class TestAttachSampleValuesRespectsRestriction:
    def test_restricted_column_is_never_sampled(self):
        table = TableSchemaInfo(
            table_name="DimCustomer",
            columns=(
                ColumnInfo(
                    name="MaritalStatus",
                    type="VARCHAR(1)",
                    nullable=True,
                    is_primary_key=False,
                ),
            ),
            foreign_keys=(),
            ddl="CREATE TABLE DimCustomer (\n    MaritalStatus VARCHAR(1)\n);",
        )
        engine = _StubEngine([("M",), ("S",)])
        classifications: dict[tuple[str, str], SensitivityTier] = {
            ("DimCustomer", "MaritalStatus"): "restricted"
        }

        result = attach_sample_values(engine, [table], sensitive_columns=classifications)

        # No sample-value comment should have been added -- the column
        # would otherwise easily qualify (short VARCHAR, 2 distinct values).
        assert "-- e.g." not in result[0].ddl

    def test_unrestricted_qualifying_column_is_still_sampled(self):
        """Sanity check against over-blocking: a column with no
        classification entry at all must sample exactly as before this
        feature existed."""
        table = TableSchemaInfo(
            table_name="DimProduct",
            columns=(
                ColumnInfo(
                    name="ProductLine", type="VARCHAR(2)", nullable=True, is_primary_key=False
                ),
            ),
            foreign_keys=(),
            ddl="CREATE TABLE DimProduct (\n    ProductLine VARCHAR(2)\n);",
        )
        engine = _StubEngine([("M",), ("R",)])

        result = attach_sample_values(engine, [table], sensitive_columns={})

        assert "-- e.g." in result[0].ddl
        assert "M" in result[0].ddl and "R" in result[0].ddl
