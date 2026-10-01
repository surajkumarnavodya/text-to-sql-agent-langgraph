"""Unit tests for onboarding/golden_questions.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`) -- the "golden questions" stage.
Fully offline: plain `SemanticLabel`/`InferredRelationship` dataclasses in,
plain `GoldenQuestionCandidate` dataclasses out, no engine, no LLM call
(this module is deliberately template-based, never LLM-generated).
"""

from __future__ import annotations

from db.relationship_inference import InferredRelationship
from onboarding.golden_questions import generate_candidate_questions
from onboarding.semantic_inference import SemanticLabel


def _label(table, column, label, confidence=0.9, is_ambiguous=False):
    return SemanticLabel(
        table_name=table,
        column_name=column,
        label=label,
        confidence=confidence,
        is_ambiguous=is_ambiguous,
        evidence=(),
    )


class TestCountQuestions:
    def test_one_count_question_per_table_sorted_by_name(self):
        labels = [_label("Zebra", "Id", "identifier"), _label("Apple", "Id", "identifier")]
        candidates = generate_candidate_questions(labels)
        count_candidates = [c for c in candidates if c.question_type == "count"]
        assert [c.tables_involved for c in count_candidates] == [("Apple",), ("Zebra",)]
        assert count_candidates[0].candidate_sql == "SELECT COUNT(*) FROM Apple"
        assert count_candidates[0].confidence == 0.9


class TestAggregateAndTopNQuestions:
    def test_measure_category_pair_produces_aggregate_and_top_n(self):
        labels = [
            _label("Sales", "Amount", "measure"),
            _label("Sales", "Region", "category"),
        ]
        candidates = generate_candidate_questions(labels)
        aggregate = next(c for c in candidates if c.question_type == "aggregate_by_category")
        top_n = next(c for c in candidates if c.question_type == "top_n")

        assert aggregate.candidate_sql == "SELECT Region, SUM(Amount) FROM Sales GROUP BY Region"
        assert "total Amount by Region" in aggregate.question
        assert aggregate.confidence == 0.75

        assert top_n.candidate_sql == (
            "SELECT Region, SUM(Amount) AS total FROM Sales GROUP BY Region ORDER BY total DESC"
        )
        assert top_n.confidence == 0.65

    def test_ambiguous_measure_or_category_dampens_confidence(self):
        labels = [
            _label("Sales", "Amount", "measure", is_ambiguous=True),
            _label("Sales", "Region", "category", is_ambiguous=False),
        ]
        candidates = generate_candidate_questions(labels)
        aggregate = next(c for c in candidates if c.question_type == "aggregate_by_category")
        assert aggregate.confidence == round(0.75 * 0.9, 4)

    def test_no_pair_without_both_a_measure_and_a_category(self):
        labels = [_label("Sales", "Amount", "measure")]
        candidates = generate_candidate_questions(labels)
        assert all(c.question_type not in {"aggregate_by_category", "top_n"} for c in candidates)

    def test_multiple_measures_and_categories_produce_the_cross_product(self):
        labels = [
            _label("Sales", "Amount", "measure"),
            _label("Sales", "Quantity", "measure"),
            _label("Sales", "Region", "category"),
            _label("Sales", "Channel", "category"),
        ]
        candidates = generate_candidate_questions(labels)
        aggregates = [c for c in candidates if c.question_type == "aggregate_by_category"]
        assert len(aggregates) == 4  # 2 measures x 2 categories


class TestJoinCountQuestions:
    def test_one_join_count_per_relationship(self):
        relationship = InferredRelationship(
            source_table="Orders",
            source_columns=("CustomerId",),
            target_table="Customers",
            target_columns=("Id",),
            relationship_type="many_to_one",
            confidence=0.8,
            evidence=(),
        )
        candidates = generate_candidate_questions([], relationships=[relationship])
        join_candidate = next(c for c in candidates if c.question_type == "join_count")

        assert join_candidate.tables_involved == ("Orders", "Customers")
        assert join_candidate.candidate_sql == (
            "SELECT t.Id, COUNT(*) FROM Orders s JOIN Customers t "
            "ON s.CustomerId = t.Id GROUP BY t.Id"
        )
        assert join_candidate.confidence == round(0.6 * 0.8, 4)

    def test_no_relationships_means_no_join_questions(self):
        candidates = generate_candidate_questions([_label("Orders", "Id", "identifier")])
        assert all(c.question_type != "join_count" for c in candidates)


class TestMaxQuestionsCap:
    def test_cap_preserves_earlier_shapes_first(self):
        labels = [
            _label("Sales", "Amount", "measure"),
            _label("Sales", "Region", "category"),
        ]
        relationship = InferredRelationship(
            source_table="Sales",
            source_columns=("RegionId",),
            target_table="Regions",
            target_columns=("Id",),
            relationship_type="many_to_one",
            confidence=0.9,
            evidence=(),
        )
        candidates = generate_candidate_questions(
            labels, relationships=[relationship], max_questions=1
        )
        assert len(candidates) == 1
        assert candidates[0].question_type == "count"

    def test_default_cap_is_twenty(self):
        labels = [_label(f"Table{i}", "Id", "identifier") for i in range(30)]
        candidates = generate_candidate_questions(labels)
        assert len(candidates) == 20
