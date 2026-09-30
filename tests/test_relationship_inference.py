"""Unit tests for db/relationship_inference.py (Prompt 07,
`07_RELATIONSHIP_INTELLIGENCE_CONTRACT.md`).

`TestInferRelationships*` cover the structural (always-on, no-query) path
against exactly the scenarios the prompt names: a correct/already-declared
FK (must not be re-proposed as a duplicate candidate), a missing FK
(correctly inferred), misleading names (rejected), incompatible types
(rejected), one-to-many vs. one-to-one direction/classification, and
many-to-many via a junction table (never inferred as a direct FK between
the two outer tables). `TestVerifyCandidatesWithData` covers the opt-in,
data-driven refinement pass (null-heavy keys, value overlap, fail-open on
a query error) against a mocked `Engine` -- no real database.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy.exc import SQLAlchemyError

from agent.provenance import DataTruthLevel
from db.relationship_inference import (
    InferredRelationship,
    RelationshipEvidence,
    infer_relationships,
    verify_candidates_with_data,
)
from db.schema_introspection import ColumnInfo, ForeignKeyInfo, TableSchemaInfo


def _table(name, columns, foreign_keys=(), unique_constraints=(), is_view=False):
    return TableSchemaInfo(
        table_name=name,
        columns=tuple(columns),
        foreign_keys=tuple(foreign_keys),
        ddl="",
        is_view=is_view,
        unique_constraints=tuple(unique_constraints),
    )


def _col(name, type_="INTEGER", nullable=False, is_primary_key=False):
    return ColumnInfo(name=name, type=type_, nullable=nullable, is_primary_key=is_primary_key)


class TestInferRelationshipsFindsAMissingFk:
    def test_missing_fk_is_correctly_inferred(self):
        dim_customer = _table("DimCustomer", [_col("CustomerKey", is_primary_key=True)])
        fact_sales = _table(
            "FactInternetSales",
            [_col("SalesOrderNumber", "VARCHAR(20)", is_primary_key=True), _col("CustomerKey")],
        )

        candidates = infer_relationships([dim_customer, fact_sales])

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.source_table == "FactInternetSales"
        assert candidate.source_columns == ("CustomerKey",)
        assert candidate.target_table == "DimCustomer"
        assert candidate.target_columns == ("CustomerKey",)
        assert candidate.relationship_type == "many_to_one"
        assert candidate.truth_level == DataTruthLevel.AI_INFERENCE
        assert candidate.confidence == 1.0
        signals = {e.signal for e in candidate.evidence}
        assert signals == {"name_similarity", "type_compatibility", "target_uniqueness"}

    def test_naming_convention_suffix_match_scores_lower_than_exact_match(self):
        customer = _table("Customers", [_col("Id", is_primary_key=True)])
        orders = _table("Orders", [_col("OrderId", is_primary_key=True), _col("CustomerId")])

        candidates = infer_relationships([customer, orders])

        assert len(candidates) == 1
        assert candidates[0].confidence < 1.0
        assert candidates[0].confidence >= 0.6


class TestInferRelationshipsDoesNotDuplicateADeclaredFk:
    def test_a_column_already_part_of_a_real_fk_is_never_re_proposed(self):
        dim_customer = _table("DimCustomer", [_col("CustomerKey", is_primary_key=True)])
        fact_sales = _table(
            "FactInternetSales",
            [_col("SalesOrderNumber", "VARCHAR(20)", is_primary_key=True), _col("CustomerKey")],
            foreign_keys=[
                ForeignKeyInfo(
                    constrained_columns=("CustomerKey",),
                    referred_table="DimCustomer",
                    referred_columns=("CustomerKey",),
                )
            ],
        )

        candidates = infer_relationships([dim_customer, fact_sales])

        assert candidates == []


class TestInferRelationshipsRejectsMisleadingNames:
    def test_type_compatible_but_unrelated_column_name_is_not_proposed(self):
        dim_product = _table(
            "DimProduct", [_col("ProductKey", is_primary_key=True), _col("Weight")]
        )
        fact_sales = _table(
            "FactInternetSales",
            [_col("SalesOrderNumber", "VARCHAR(20)", is_primary_key=True), _col("Quantity")],
        )

        candidates = infer_relationships([dim_product, fact_sales])

        assert candidates == []

    def test_no_candidate_without_any_name_signal_regardless_of_type_match(self):
        a = _table("TableA", [_col("Key1", is_primary_key=True)])
        b = _table("TableB", [_col("SomeUnrelatedNumber")])

        assert infer_relationships([a, b]) == []


class TestInferRelationshipsRejectsIncompatibleTypes:
    def test_name_match_with_incompatible_types_is_rejected(self):
        customer = _table("Customer", [_col("Id", is_primary_key=True)])
        orders = _table(
            "Orders",
            [_col("OrderId", is_primary_key=True), _col("CustomerId", "VARCHAR(10)")],
        )

        assert infer_relationships([customer, orders]) == []


class TestInferRelationshipsDirectionAndCardinality:
    def test_one_to_one_when_source_column_is_itself_unique(self):
        customer = _table("Customers", [_col("Id", is_primary_key=True)])
        profile = _table(
            "CustomerProfile",
            [_col("CustomerId", is_primary_key=True)],
        )

        candidates = infer_relationships([customer, profile])

        assert len(candidates) == 1
        assert candidates[0].relationship_type == "one_to_one"

    def test_many_to_one_when_source_column_is_not_unique(self):
        customer = _table("Customers", [_col("Id", is_primary_key=True)])
        orders = _table("Orders", [_col("OrderId", is_primary_key=True), _col("CustomerId")])

        candidates = infer_relationships([customer, orders])

        assert len(candidates) == 1
        assert candidates[0].relationship_type == "many_to_one"


class TestInferRelationshipsManyToManyViaJunctionTable:
    def test_no_direct_relationship_inferred_between_the_two_outer_tables(self):
        student = _table("Student", [_col("StudentId", is_primary_key=True)])
        course = _table("Course", [_col("CourseId", is_primary_key=True)])
        enrollment = _table("Enrollment", [_col("StudentId"), _col("CourseId")])

        candidates = infer_relationships([student, course, enrollment])

        direct = [
            c for c in candidates if {c.source_table, c.target_table} == {"Student", "Course"}
        ]
        assert direct == []

    def test_junction_table_still_gets_both_many_to_one_relationships(self):
        student = _table("Student", [_col("StudentId", is_primary_key=True)])
        course = _table("Course", [_col("CourseId", is_primary_key=True)])
        enrollment = _table("Enrollment", [_col("StudentId"), _col("CourseId")])

        candidates = infer_relationships([student, course, enrollment])

        targets = {(c.source_table, c.target_table) for c in candidates}
        assert ("Enrollment", "Student") in targets
        assert ("Enrollment", "Course") in targets
        assert all(c.relationship_type == "many_to_one" for c in candidates)


class TestInferRelationshipsIgnoresViews:
    def test_a_view_is_never_a_source_or_target(self):
        customer = _table("Customers", [_col("Id", is_primary_key=True)])
        a_view = _table("CustomerView", [_col("CustomerId")], is_view=True)

        assert infer_relationships([customer, a_view]) == []


class TestInferRelationshipsMinConfidence:
    def test_candidates_below_min_confidence_are_dropped(self):
        customer = _table("Customers", [_col("Id", is_primary_key=True)])
        orders = _table("Orders", [_col("OrderId", is_primary_key=True), _col("CustomerId")])

        assert infer_relationships([customer, orders], min_confidence=0.99) == []
        assert infer_relationships([customer, orders], min_confidence=0.6) != []


def _candidate(**overrides) -> InferredRelationship:
    base = dict(
        source_table="Orders",
        source_columns=("CustomerId",),
        target_table="Customers",
        target_columns=("Id",),
        relationship_type="many_to_one",
        confidence=0.9,
        evidence=(RelationshipEvidence("name_similarity", 0.85, "suffix match"),),
    )
    base.update(overrides)
    return InferredRelationship(**base)


def _mock_engine(null_fraction_rows, overlap_source_rows, overlap_target_rows):
    """`verify_candidates_with_data` makes up to three `fetchmany()` calls
    per candidate, in this exact order:
      1. `_sample_null_fraction`'s own sample of the source column.
      2. `_sample_value_overlap`'s own (separate) non-NULL sample of the
         source column.
      3. `_sample_value_overlap`'s lookup of which sampled values exist in
         the target column -- skipped entirely if (2) came back empty.
    """
    connection = MagicMock()
    results = iter([null_fraction_rows, overlap_source_rows, overlap_target_rows])
    connection.execute.return_value.fetchmany.side_effect = lambda n: next(results)

    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    engine.dialect.identifier_preparer.quote.side_effect = lambda name: f'"{name}"'
    return engine


class TestVerifyCandidatesWithDataNullHeavyKey:
    def test_heavily_null_source_column_reduces_confidence(self):
        candidate = _candidate(confidence=0.9)
        null_fraction_sample = [(None,)] * 8 + [(1,), (2,)]  # 80% NULL
        engine = _mock_engine(
            null_fraction_rows=null_fraction_sample,
            overlap_source_rows=[(1,), (2,)],
            overlap_target_rows=[(1,), (2,)],
        )

        refined = verify_candidates_with_data([candidate], engine)

        assert refined[0].confidence < candidate.confidence
        null_evidence = next(e for e in refined[0].evidence if e.signal == "null_fraction")
        assert "80%" in null_evidence.detail

    def test_low_null_fraction_does_not_penalize_confidence(self):
        candidate = _candidate(confidence=0.9)
        null_fraction_sample = [(1,)] * 9 + [(None,)]  # 10% NULL
        engine = _mock_engine(
            null_fraction_rows=null_fraction_sample,
            overlap_source_rows=[(1,)],
            overlap_target_rows=[(1,)],
        )

        refined = verify_candidates_with_data([candidate], engine)

        null_evidence = next(e for e in refined[0].evidence if e.signal == "null_fraction")
        assert "10%" in null_evidence.detail


class TestVerifyCandidatesWithDataValueOverlap:
    def test_full_value_overlap_raises_confidence_toward_one(self):
        candidate = _candidate(confidence=0.7)
        engine = _mock_engine(
            null_fraction_rows=[(1,), (2,), (3,)],
            overlap_source_rows=[(1,), (2,), (3,)],
            overlap_target_rows=[(1,), (2,), (3,)],
        )

        refined = verify_candidates_with_data([candidate], engine)

        overlap_evidence = next(e for e in refined[0].evidence if e.signal == "value_overlap")
        assert overlap_evidence.score == 1.0
        assert refined[0].confidence > candidate.confidence

    def test_zero_value_overlap_sharply_lowers_confidence(self):
        candidate = _candidate(confidence=0.9)
        engine = _mock_engine(
            null_fraction_rows=[(1,), (2,), (3,)],
            overlap_source_rows=[(1,), (2,), (3,)],
            overlap_target_rows=[],  # none of the sampled source values exist in the target
        )

        refined = verify_candidates_with_data([candidate], engine)

        overlap_evidence = next(e for e in refined[0].evidence if e.signal == "value_overlap")
        assert overlap_evidence.score == 0.0
        assert refined[0].confidence < candidate.confidence


class TestVerifyCandidatesWithDataFailsOpen:
    def test_a_query_error_leaves_the_candidate_unchanged_not_dropped(self):
        candidate = _candidate()
        engine = MagicMock()
        engine.connect.side_effect = SQLAlchemyError("connection refused")

        refined = verify_candidates_with_data([candidate], engine)

        assert refined == [candidate]

    def test_confidence_never_exceeds_one(self):
        candidate = _candidate(confidence=0.95)
        engine = _mock_engine(
            null_fraction_rows=[(1,)],
            overlap_source_rows=[(1,)],
            overlap_target_rows=[(1,)],
        )

        refined = verify_candidates_with_data([candidate], engine)

        assert refined[0].confidence <= 1.0
