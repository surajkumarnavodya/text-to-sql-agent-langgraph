"""Unit tests for onboarding/semantic_inference.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`) -- the "semantic inference" +
"ambiguity detection" stages. Fully offline: plain dataclasses in, plain
dataclasses out, no engine, no database.
"""

from __future__ import annotations

from db.relationship_inference import InferredRelationship
from db.schema_introspection import ColumnInfo, ForeignKeyInfo, TableSchemaInfo
from onboarding.profiling import ColumnProfile
from onboarding.semantic_inference import infer_semantic_labels


def _table(name, columns, foreign_keys=()):
    return TableSchemaInfo(
        table_name=name, columns=tuple(columns), foreign_keys=tuple(foreign_keys), ddl=""
    )


def _col(name, type_="INTEGER", is_primary_key=False):
    return ColumnInfo(name=name, type=type_, nullable=True, is_primary_key=is_primary_key)


def _profile(table, column, distinct_count, uniqueness_ratio, top_values=()):
    return ColumnProfile(
        table_name=table,
        column_name=column,
        total_rows=100,
        null_count=0,
        null_fraction=0.0,
        distinct_count=distinct_count,
        uniqueness_ratio=uniqueness_ratio,
        min_value=None,
        max_value=None,
        avg_value=None,
        percentiles=None,
        top_values=top_values,
        freshness_days=None,
    )


class TestInferSemanticLabelsBasicRoles:
    def test_primary_key_is_identifier(self):
        table = _table("Orders", [_col("OrderId", is_primary_key=True)])
        labels = infer_semantic_labels([table])
        assert labels[0].label == "identifier"
        assert labels[0].confidence == 1.0
        assert labels[0].is_ambiguous is False

    def test_declared_fk_column_is_foreign_key(self):
        table = _table(
            "Orders",
            [_col("OrderId", is_primary_key=True), _col("CustomerId")],
            foreign_keys=[ForeignKeyInfo(("CustomerId",), "Customers", ("Id",))],
        )
        labels = infer_semantic_labels([table])
        fk_label = next(lbl for lbl in labels if lbl.column_name == "CustomerId")
        assert fk_label.label == "foreign_key"

    def test_inferred_relationship_source_column_is_foreign_key(self):
        table = _table("Orders", [_col("OrderId", is_primary_key=True), _col("CustomerId")])
        candidate = InferredRelationship(
            source_table="Orders",
            source_columns=("CustomerId",),
            target_table="Customers",
            target_columns=("Id",),
            relationship_type="many_to_one",
            confidence=0.9,
            evidence=(),
        )
        labels = infer_semantic_labels([table], relationships=[candidate])
        fk_label = next(lbl for lbl in labels if lbl.column_name == "CustomerId")
        assert fk_label.label == "foreign_key"

    def test_boolean_column_is_flag(self):
        table = _table("Orders", [_col("IsPaid", "BOOLEAN")])
        labels = infer_semantic_labels([table])
        assert labels[0].label == "flag"

    def test_datetime_column_is_timestamp(self):
        table = _table("Orders", [_col("OrderDate", "DATETIME2")])
        labels = infer_semantic_labels([table])
        assert labels[0].label == "timestamp"

    def test_views_are_skipped(self):
        table = TableSchemaInfo(
            table_name="v", columns=(_col("x"),), foreign_keys=(), ddl="", is_view=True
        )
        assert infer_semantic_labels([table]) == []


class TestInferSemanticLabelsUsingProfiles:
    def test_high_uniqueness_numeric_non_key_is_measure(self):
        table = _table("Sales", [_col("Amount", "DECIMAL")])
        profile = _profile("Sales", "Amount", distinct_count=95, uniqueness_ratio=0.95)
        labels = infer_semantic_labels([table], column_profiles={("Sales", "Amount"): profile})
        assert labels[0].label == "measure"

    def test_low_cardinality_text_with_top_values_is_category(self):
        table = _table("Sales", [_col("Region", "VARCHAR(50)")])
        profile = _profile(
            "Sales",
            "Region",
            distinct_count=2,
            uniqueness_ratio=0.02,
            top_values=(("East", 50), ("West", 50)),
        )
        labels = infer_semantic_labels([table], column_profiles={("Sales", "Region"): profile})
        assert labels[0].label == "category"

    def test_high_cardinality_text_with_no_top_values_is_free_text(self):
        table = _table("Sales", [_col("Description", "VARCHAR(500)")])
        profile = _profile("Sales", "Description", distinct_count=100, uniqueness_ratio=1.0)
        labels = infer_semantic_labels([table], column_profiles={("Sales", "Description"): profile})
        assert labels[0].label == "free_text"

    def test_unprofiled_text_column_still_gets_a_low_confidence_label(self):
        table = _table("Sales", [_col("Notes", "VARCHAR(500)")])
        labels = infer_semantic_labels([table])
        assert labels[0].label == "free_text"
        assert labels[0].confidence < 0.65


class TestInferSemanticLabelsAmbiguity:
    def test_numeric_low_cardinality_column_is_ambiguous_between_measure_and_category(self):
        """A numeric column with a moderate distinct count (<=20, so
        'category' is plausible) and uniqueness above 0.5 (so 'measure'
        is also plausible) is a genuine near-tie between the two --
        exactly the case the acceptance criterion names. 'measure' (0.7)
        edges out 'category' (0.65), but the 0.05 margin is well under
        the ambiguity threshold."""
        table = _table("Sales", [_col("RatingCode", "INTEGER")])
        profile = _profile("Sales", "RatingCode", distinct_count=10, uniqueness_ratio=0.6)
        labels = infer_semantic_labels([table], column_profiles={("Sales", "RatingCode"): profile})
        assert labels[0].label == "measure"
        assert labels[0].is_ambiguous is True

    def test_clear_winner_is_not_ambiguous(self):
        table = _table("Orders", [_col("OrderId", is_primary_key=True)])
        labels = infer_semantic_labels([table])
        assert labels[0].is_ambiguous is False
