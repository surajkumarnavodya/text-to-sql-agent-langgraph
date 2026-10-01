"""Unit tests for `analytics/root_cause.py` -- Prompt 14
(`14_ANOMALY_ROOT_CAUSE_CONTRACT.md`)'s evidence-based root-cause/
contribution engine.

Covers the full 7-step flow on a clear single-dominant-contributor case,
a broadly-distributed-change case (insufficient evidence by
construction), empty inputs, no dimension columns, a near-zero-magnitude
case, a new/dropped contributor, and a multidimensional (composite-label)
breakdown -- matching the prompt's own explicit testing requirement
("anomalies, no-anomaly cases, insufficient data and contribution
calculations").
"""

from __future__ import annotations

from analytics.root_cause import investigate_root_cause

from config.settings import Settings

_COLUMNS = ["Region", "Revenue"]


class TestSufficientEvidence:
    def test_single_dominant_contributor(self):
        baseline = [("West", 100.0), ("East", 100.0), ("South", 100.0)]
        current = [("West", 500.0), ("East", 105.0), ("South", 98.0)]
        result = investigate_root_cause(current, baseline, _COLUMNS, ["Region"], "Revenue")

        assert result.has_sufficient_evidence is True
        assert result.magnitude == 403.0
        assert result.baseline_total == 300.0
        assert result.current_total == 703.0
        assert len(result.contributors) == 1
        contributor = result.contributors[0]
        assert contributor.label == "West"
        assert contributor.contribution == 400.0
        assert contributor.contribution_percent == round(100 * 400.0 / 403.0, 1)
        assert contributor.rank == 1
        assert result.confidence is not None
        assert 0.0 < result.confidence <= 1.0
        assert result.engine_version

    def test_contributors_are_ranked_by_absolute_contribution_descending(self):
        baseline = [("A", 100.0), ("B", 100.0), ("C", 100.0)]
        current = [("A", 150.0), ("B", 300.0), ("C", 90.0)]
        result = investigate_root_cause(current, baseline, _COLUMNS, ["Region"], "Revenue")
        assert result.has_sufficient_evidence is True
        ranks = [c.rank for c in result.contributors]
        assert ranks == sorted(ranks)
        assert result.contributors[0].label == "B"

    def test_new_contributor_absent_from_baseline(self):
        baseline = [("West", 100.0), ("East", 100.0)]
        current = [("West", 100.0), ("East", 100.0), ("North", 500.0)]
        result = investigate_root_cause(current, baseline, _COLUMNS, ["Region"], "Revenue")
        assert result.has_sufficient_evidence is True
        north = next(c for c in result.contributors if c.label == "North")
        assert north.baseline_value == 0.0
        assert north.current_value == 500.0
        assert north.contribution == 500.0

    def test_dropped_contributor_absent_from_current(self):
        baseline = [("West", 100.0), ("East", 100.0), ("North", 500.0)]
        current = [("West", 100.0), ("East", 100.0)]
        result = investigate_root_cause(current, baseline, _COLUMNS, ["Region"], "Revenue")
        assert result.has_sufficient_evidence is True
        north = next(c for c in result.contributors if c.label == "North")
        assert north.current_value == 0.0
        assert north.contribution == -500.0

    def test_multidimensional_composite_label(self):
        columns = ["Category", "Region", "Revenue"]
        baseline = [("Bikes", "West", 100.0), ("Bikes", "East", 100.0), ("Clothing", "West", 100.0)]
        current = [("Bikes", "West", 600.0), ("Bikes", "East", 100.0), ("Clothing", "West", 90.0)]
        result = investigate_root_cause(
            current, baseline, columns, ["Category", "Region"], "Revenue"
        )
        assert result.has_sufficient_evidence is True
        assert result.contributors[0].label == "Bikes / West"


class TestInsufficientEvidence:
    def test_broadly_distributed_change_has_no_single_contributor(self):
        baseline = [(f"R{i}", 100.0) for i in range(12)]
        current = [(f"R{i}", 108.0) for i in range(12)]
        result = investigate_root_cause(current, baseline, _COLUMNS, ["Region"], "Revenue")
        assert result.has_sufficient_evidence is False
        assert result.contributors == ()
        assert result.confidence is None
        assert any("broadly distributed" in reason for reason in result.limitations)
        # Magnitude is still reported even without a named cause.
        assert result.magnitude == 96.0

    def test_empty_current_rows(self):
        result = investigate_root_cause([], [("West", 100.0)], _COLUMNS, ["Region"], "Revenue")
        assert result.has_sufficient_evidence is False
        assert result.magnitude is None
        assert any("nothing to compare" in reason for reason in result.limitations)

    def test_empty_baseline_rows(self):
        result = investigate_root_cause([("West", 100.0)], [], _COLUMNS, ["Region"], "Revenue")
        assert result.has_sufficient_evidence is False

    def test_no_dimension_columns_given(self):
        baseline = [("West", 100.0)]
        current = [("West", 200.0)]
        result = investigate_root_cause(current, baseline, _COLUMNS, [], "Revenue")
        assert result.has_sufficient_evidence is False
        assert result.magnitude == 100.0  # still computed
        assert any("nothing to break the change down by" in reason for reason in result.limitations)

    def test_zero_magnitude_nothing_to_explain(self):
        baseline = [("West", 100.0), ("East", 100.0)]
        current = [("West", 100.0), ("East", 100.0)]
        result = investigate_root_cause(current, baseline, _COLUMNS, ["Region"], "Revenue")
        assert result.has_sufficient_evidence is False
        assert result.magnitude == 0.0
        assert any("no change" in reason for reason in result.limitations)

    def test_unknown_value_column(self):
        result = investigate_root_cause(
            [("West", 100.0)], [("West", 90.0)], _COLUMNS, ["Region"], "NotAColumn"
        )
        assert result.has_sufficient_evidence is False
        assert any("not found" in reason for reason in result.limitations)

    def test_unknown_dimension_column(self):
        result = investigate_root_cause(
            [("West", 100.0)], [("West", 90.0)], _COLUMNS, ["NotAColumn"], "Revenue"
        )
        assert result.has_sufficient_evidence is False
        assert any("dimension column" in reason for reason in result.limitations)


class TestValidationThreshold:
    def test_contributors_below_the_bar_are_excluded_and_counted(self):
        settings = Settings(root_cause_min_contribution_percent=50.0)
        baseline = [("A", 100.0), ("B", 100.0), ("C", 100.0)]
        current = [("A", 140.0), ("B", 110.0), ("C", 100.0)]  # magnitude = 50
        result = investigate_root_cause(
            current, baseline, _COLUMNS, ["Region"], "Revenue", settings
        )
        # A's own contribution is 40/50 = 80% -> clears a 50% bar.
        assert result.has_sufficient_evidence is True
        assert len(result.contributors) == 1
        assert result.contributors[0].label == "A"
        assert any("did not individually explain" in reason for reason in result.limitations)

    def test_lower_threshold_admits_more_contributors(self):
        settings = Settings(root_cause_min_contribution_percent=5.0)
        baseline = [("A", 100.0), ("B", 100.0), ("C", 100.0)]
        current = [("A", 140.0), ("B", 110.0), ("C", 100.0)]
        result = investigate_root_cause(
            current, baseline, _COLUMNS, ["Region"], "Revenue", settings
        )
        assert len(result.contributors) == 2


class TestFormulaAndVersionMetadata:
    def test_formula_and_engine_version_present_on_sufficient_evidence(self):
        baseline = [("A", 100.0), ("B", 100.0)]
        current = [("A", 500.0), ("B", 100.0)]
        result = investigate_root_cause(current, baseline, _COLUMNS, ["Region"], "Revenue")
        assert result.formula
        assert result.engine_version

    def test_formula_present_even_on_insufficient_evidence(self):
        result = investigate_root_cause([], [], _COLUMNS, ["Region"], "Revenue")
        assert result.formula
