"""Unit tests for `recommendation/engine.py` -- Prompt 17
(`17_RECOMMENDATION_ENGINE_CONTRACT.md`)'s Evidence-First Recommendation
Engine.

One class per category rule (each proving a "supported" case that
produces a `Recommendation` and an "unsupported" case that doesn't),
plus dedicated classes for the pipeline-level evidence-validation,
confidence-floor, and authorization invariants -- matching the prompt's
own explicit "test supported and unsupported recommendations, evidence
validation and authorization" requirement.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from analytics.engine import compute_analytics_result
from analytics.models import ContributorStat, RootCauseResult
from recommendation.engine import (
    RecommendationInputs,
    _Candidate,
    _finalize_candidate,
    generate_recommendations,
)
from recommendation.models import RecommendationCategory, RecommendationKind

from agent.provenance import DataTruthLevel, ProvenancedClaim
from config.settings import Settings
from db.query_cost import CostEstimate
from db.query_store import (
    QueryStoreAvailability,
    QueryStoreFindings,
    QueryStoreQueryStats,
    QueryStoreRegression,
)
from security.secrets import SecretStr


def _settings(**overrides) -> Settings:
    base = Settings(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        db_type="postgresql",
        db_host="db.example.com",
        db_name="mydb",
        db_user="reader",
        db_password=SecretStr("secret"),
        db_connection_string=None,
        db_schema=None,
        chroma_persist_dir=Path("/tmp/chroma"),
    )
    return Settings(**{**base.__dict__, **overrides})


_REVENUE_DECLINE = compute_analytics_result(
    ["OrderYear", "TotalRevenue"],
    [(2021, 1000.0), (2022, 800.0), (2023, 400.0)],
    _settings(),
)
_CUSTOMER_CONCENTRATION = compute_analytics_result(
    ["CustomerName", "TotalSpend"],
    [("Acme Corp", 900.0), ("Globex", 60.0), ("Initech", 40.0)],
    _settings(),
)
_NULL_HEAVY = compute_analytics_result(
    ["Email"],
    [("a@example.com",), (None,), (None,), (None,), (None,)],
    _settings(),
)


class TestAnomalyRule:
    def test_supported_anomaly_produces_a_recommendation(self):
        rows = [(2015 + i, 100.0) for i in range(10)]
        rows[-1] = (2024, 10_000.0)
        result = compute_analytics_result(["Year", "Value"], rows, _settings())
        inputs = RecommendationInputs(analytics_result=result)

        recs = generate_recommendations(inputs, _settings())

        anomaly_recs = [r for r in recs if r.category == RecommendationCategory.ANOMALY]
        assert anomaly_recs, "expected an anomaly recommendation for a sharp spike"
        rec = anomaly_recs[0]
        assert rec.evidence
        assert all(e.level == DataTruthLevel.DATABASE_FACT for e in rec.evidence)
        assert rec.claim.level == DataTruthLevel.AI_INFERENCE
        assert rec.rule_or_model == "recommendation.engine.AnomalyRule"
        assert rec.confidence is not None and 0.0 <= rec.confidence <= 1.0

    def test_no_anomaly_means_no_anomaly_recommendation(self):
        rows = [(2015 + i, 100.0) for i in range(6)]
        result = compute_analytics_result(["Year", "Value"], rows, _settings())
        inputs = RecommendationInputs(analytics_result=result)

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.ANOMALY]


class TestDataQualityRule:
    def test_supported_high_null_rate_produces_a_recommendation(self):
        inputs = RecommendationInputs(analytics_result=_NULL_HEAVY)

        recs = generate_recommendations(inputs, _settings())

        dq = [r for r in recs if r.category == RecommendationCategory.DATA_QUALITY]
        assert dq
        assert dq[0].affected_entity == "Email"
        assert dq[0].measurable_impact is not None
        assert dq[0].evidence

    def test_unsupported_below_null_rate_threshold(self):
        result = compute_analytics_result(
            ["Email"], [("a@example.com",), ("b@example.com",)], _settings()
        )
        inputs = RecommendationInputs(analytics_result=result)

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.DATA_QUALITY]


class TestRevenueRule:
    def test_supported_declining_revenue_produces_a_recommendation(self):
        inputs = RecommendationInputs(analytics_result=_REVENUE_DECLINE)

        recs = generate_recommendations(inputs, _settings())

        revenue = [r for r in recs if r.category == RecommendationCategory.REVENUE]
        assert revenue
        assert revenue[0].affected_entity == "TotalRevenue"
        assert "fell" in revenue[0].claim.value
        assert "heuristic" in " ".join(revenue[0].limitations).lower()

    def test_unsupported_when_column_name_does_not_match_revenue_keywords(self):
        result = compute_analytics_result(
            ["OrderYear", "WidgetCount"],
            [(2021, 1000.0), (2022, 800.0), (2023, 400.0)],
            _settings(),
        )
        inputs = RecommendationInputs(analytics_result=result)

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.REVENUE]

    def test_unsupported_when_growth_direction_is_up(self):
        result = compute_analytics_result(
            ["OrderYear", "TotalRevenue"],
            [(2021, 400.0), (2022, 800.0), (2023, 1000.0)],
            _settings(),
        )
        inputs = RecommendationInputs(analytics_result=result)

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.REVENUE]


class TestCustomerAndProductConcentrationRules:
    def test_supported_customer_concentration_produces_a_recommendation(self):
        inputs = RecommendationInputs(analytics_result=_CUSTOMER_CONCENTRATION)

        recs = generate_recommendations(inputs, _settings())

        customer = [r for r in recs if r.category == RecommendationCategory.CUSTOMER]
        assert customer
        assert customer[0].affected_entity == "Acme Corp"

    def test_unsupported_below_concentration_threshold(self):
        result = compute_analytics_result(
            ["CustomerName", "TotalSpend"],
            [("Acme Corp", 40.0), ("Globex", 30.0), ("Initech", 30.0)],
            _settings(),
        )
        inputs = RecommendationInputs(analytics_result=result)

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.CUSTOMER]

    def test_product_concentration_keyword_gated_separately_from_customer(self):
        result = compute_analytics_result(
            ["ProductCategory", "UnitsSold"],
            [("Bikes", 900.0), ("Accessories", 60.0), ("Clothing", 40.0)],
            _settings(),
        )
        inputs = RecommendationInputs(analytics_result=result)

        recs = generate_recommendations(inputs, _settings())

        product = [r for r in recs if r.category == RecommendationCategory.PRODUCT]
        customer = [r for r in recs if r.category == RecommendationCategory.CUSTOMER]
        assert product
        assert not customer


class TestOperationsRule:
    def test_supported_root_cause_produces_a_recommendation(self):
        root_cause = RootCauseResult(
            has_sufficient_evidence=True,
            magnitude=-500.0,
            magnitude_percent=-50.0,
            baseline_total=1000.0,
            current_total=500.0,
            contributors=(
                ContributorStat(
                    label="West",
                    baseline_value=600.0,
                    current_value=100.0,
                    contribution=-500.0,
                    contribution_percent=-100.0,
                    rank=1,
                ),
            ),
            confidence=0.65,
            limitations=("3 dimension value(s) excluded as noise",),
        )
        inputs = RecommendationInputs(root_cause_result=root_cause)

        recs = generate_recommendations(inputs, _settings())

        ops = [r for r in recs if r.category == RecommendationCategory.OPERATIONS]
        assert ops
        assert ops[0].affected_entity == "West"
        assert ops[0].confidence == pytest.approx(0.65)
        assert ops[0].limitations == ("3 dimension value(s) excluded as noise",)

    def test_unsupported_when_root_cause_has_insufficient_evidence(self):
        root_cause = RootCauseResult(
            has_sufficient_evidence=False,
            limitations=("no single dimension value explains enough of the change",),
        )
        inputs = RecommendationInputs(root_cause_result=root_cause)

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.OPERATIONS]


class TestDatabasePerformanceRule:
    def test_supported_high_severity_estimate_produces_a_recommendation(self):
        estimate = CostEstimate(
            estimated_rows=5_000_000.0,
            estimated_cost=12345.0,
            severity="high",
            plan_summary="Clustered Index Scan on FactInternetSales",
        )
        inputs = RecommendationInputs(cost_estimate=estimate)

        recs = generate_recommendations(inputs, _settings())

        perf = [r for r in recs if r.category == RecommendationCategory.DATABASE_PERFORMANCE]
        assert perf
        assert perf[0].confidence == pytest.approx(0.9)

    def test_unsupported_low_severity_estimate(self):
        estimate = CostEstimate(
            estimated_rows=10.0, estimated_cost=1.0, severity="low", plan_summary="Index Seek"
        )
        inputs = RecommendationInputs(cost_estimate=estimate)

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.DATABASE_PERFORMANCE]


def _query_store_stats(**overrides) -> QueryStoreQueryStats:
    defaults = dict(
        query_id=1,
        query_fingerprint="abc123",
        normalized_sql_preview="SELECT * FROM T WHERE x = ?",
        execution_count=50,
        avg_duration_ms=2500.0,
        avg_cpu_ms=1200.0,
        avg_logical_reads=5000.0,
        plan_count=1,
        has_forced_plan=False,
    )
    defaults.update(overrides)
    return QueryStoreQueryStats(**defaults)


def _query_store_regression(**overrides) -> QueryStoreRegression:
    defaults = dict(
        query_id=2,
        query_fingerprint="def456",
        normalized_sql_preview="SELECT * FROM U WHERE y = ?",
        baseline_avg_duration_ms=100.0,
        recent_avg_duration_ms=400.0,
        regression_factor=4.0,
        recent_execution_count=20,
        baseline_execution_count=30,
    )
    defaults.update(overrides)
    return QueryStoreRegression(**defaults)


def _query_store_findings(**overrides) -> QueryStoreFindings:
    defaults = dict(
        availability=QueryStoreAvailability(available=True, reason="available"),
        top_queries=(),
        regressions=(),
        lookback_hours=24.0,
    )
    defaults.update(overrides)
    return QueryStoreFindings(**defaults)


class TestQueryStoreHighCostRule:
    def test_supported_high_duration_query_produces_a_recommendation(self):
        findings = _query_store_findings(top_queries=(_query_store_stats(),))
        inputs = RecommendationInputs(query_store_findings=findings)

        recs = generate_recommendations(inputs, _settings())

        perf = [
            r
            for r in recs
            if r.rule_or_model == "recommendation.engine.QueryStoreHighCostRepeatedQueryRule"
        ]
        assert perf
        assert perf[0].category == RecommendationCategory.DATABASE_PERFORMANCE
        assert perf[0].affected_entity == "abc123"
        assert "secret" not in perf[0].claim.value
        assert all(e.level == DataTruthLevel.DATABASE_FACT for e in perf[0].evidence)

    def test_unsupported_below_duration_threshold(self):
        findings = _query_store_findings(top_queries=(_query_store_stats(avg_duration_ms=50.0),))
        inputs = RecommendationInputs(
            query_store_findings=findings,
        )

        recs = generate_recommendations(
            inputs, _settings(query_store_high_duration_ms_threshold=1000.0)
        )

        assert not [
            r
            for r in recs
            if r.rule_or_model == "recommendation.engine.QueryStoreHighCostRepeatedQueryRule"
        ]

    def test_unavailable_query_store_produces_nothing(self):
        findings = _query_store_findings(
            availability=QueryStoreAvailability(available=False, reason="not mssql"),
            top_queries=(_query_store_stats(),),
        )
        inputs = RecommendationInputs(query_store_findings=findings)

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.DATABASE_PERFORMANCE]

    def test_none_query_store_findings_is_a_no_op(self):
        inputs = RecommendationInputs(query_store_findings=None)
        recs = generate_recommendations(inputs, _settings())
        assert recs == ()


class TestQueryStoreRegressionRule:
    def test_supported_regression_produces_a_recommendation(self):
        findings = _query_store_findings(regressions=(_query_store_regression(),))
        inputs = RecommendationInputs(query_store_findings=findings)

        recs = generate_recommendations(inputs, _settings())

        regression_recs = [
            r for r in recs if r.rule_or_model == "recommendation.engine.QueryStoreRegressionRule"
        ]
        assert regression_recs
        assert regression_recs[0].category == RecommendationCategory.DATABASE_PERFORMANCE
        assert regression_recs[0].affected_entity == "def456"
        assert "4.0x" in regression_recs[0].claim.value

    def test_no_regressions_produces_nothing(self):
        findings = _query_store_findings(regressions=())
        inputs = RecommendationInputs(query_store_findings=findings)

        recs = generate_recommendations(inputs, _settings())

        assert not [
            r for r in recs if r.rule_or_model == "recommendation.engine.QueryStoreRegressionRule"
        ]

    def test_evidence_never_contains_raw_literal_text(self):
        """The regression's own normalized_sql_preview is already literal-
        masked by db.query_store -- this is a regression test proving the
        recommendation layer never re-introduces a literal when rendering
        its own claim/evidence text around that preview."""
        findings = _query_store_findings(
            regressions=(
                _query_store_regression(normalized_sql_preview="SELECT * FROM U WHERE Email = ?"),
            )
        )
        inputs = RecommendationInputs(query_store_findings=findings)

        recs = generate_recommendations(inputs, _settings())

        regression_recs = [
            r for r in recs if r.rule_or_model == "recommendation.engine.QueryStoreRegressionRule"
        ]
        assert "@" not in regression_recs[0].claim.value


class TestPerformanceRule:
    def test_supported_slow_stage_produces_a_recommendation(self):
        snapshot = {
            "stages": [{"stage": "generate_sql", "count": 50, "mean_ms": 2500.0, "p95_ms": 4000.0}]
        }
        inputs = RecommendationInputs(performance_snapshot=snapshot)

        recs = generate_recommendations(inputs, _settings())

        perf = [r for r in recs if r.category == RecommendationCategory.PERFORMANCE]
        assert perf
        assert perf[0].affected_entity == "generate_sql"

    def test_unsupported_fast_stage(self):
        snapshot = {
            "stages": [{"stage": "retrieve_schema", "count": 50, "mean_ms": 50.0, "p95_ms": 80.0}]
        }
        inputs = RecommendationInputs(performance_snapshot=snapshot)

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.PERFORMANCE]


class TestSecurityRule:
    def test_supported_restricted_column_hit_produces_a_recommendation(self):
        inputs = RecommendationInputs(
            restricted_column_hits=(("Employee", "SSN"),),
            caller_roles=("admin",),
        )

        recs = generate_recommendations(inputs, _settings())

        security = [r for r in recs if r.category == RecommendationCategory.SECURITY]
        assert security
        assert security[0].affected_entity == "Employee.SSN"
        assert security[0].evidence[0].level == DataTruthLevel.CONFIRMED_BUSINESS_TRUTH

    def test_no_hits_means_no_security_recommendation(self):
        inputs = RecommendationInputs(restricted_column_hits=(), caller_roles=("admin",))

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.SECURITY]


class TestAuthorization:
    """`recommendation.engine`'s own independent authorization check --
    distinct from (and, in the live graph, redundant with)
    `agent.nodes.validate_sql_node`'s pre-execution gate. See
    `recommendation.engine`'s own module docstring."""

    def test_unauthorized_viewer_never_sees_a_restricted_column_recommendation(self, monkeypatch):
        monkeypatch.setattr(
            "recommendation.engine.load_sensitive_columns",
            lambda: {("Employee", "SSN"): "restricted"},
        )
        inputs = RecommendationInputs(
            restricted_column_hits=(("Employee", "SSN"),),
            caller_roles=("viewer",),
        )

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.SECURITY]

    def test_authorized_viewer_does_see_it(self, monkeypatch):
        monkeypatch.setattr(
            "recommendation.engine.load_sensitive_columns",
            lambda: {("Employee", "SSN"): "restricted"},
        )
        inputs = RecommendationInputs(
            restricted_column_hits=(("Employee", "SSN"),),
            caller_roles=("analyst",),
        )

        recs = generate_recommendations(inputs, _settings())

        assert [r for r in recs if r.category == RecommendationCategory.SECURITY]

    def test_a_restricted_column_elsewhere_also_suppresses_a_non_security_recommendation(
        self, monkeypatch
    ):
        """The authorization check isn't SECURITY-category-specific -- a
        DATA_QUALITY candidate whose own evidence column happens to be
        classified restricted is suppressed for an unauthorized viewer too."""
        monkeypatch.setattr(
            "recommendation.engine.load_sensitive_columns",
            lambda: {("Customer", "Email"): "restricted"},
        )
        inputs = RecommendationInputs(analytics_result=_NULL_HEAVY, caller_roles=("viewer",))

        recs = generate_recommendations(inputs, _settings())

        assert not [r for r in recs if r.category == RecommendationCategory.DATA_QUALITY]


class TestEvidenceValidationAndConfidenceFloor:
    """White-box tests of `_finalize_candidate` -- the one and only
    construction point for a `Recommendation` this module produces (see
    its own docstring for why evidence/confidence are pipeline-level
    checks, not `Recommendation` model validators)."""

    def test_candidate_with_no_evidence_is_dropped(self):
        candidate = _Candidate(
            category=RecommendationCategory.ANOMALY,
            kind=RecommendationKind.ACTION,
            text="Do something.",
            rationale="because",
            evidence=(),
            rule_name="test.NoEvidenceRule",
            confidence=0.9,
        )

        result = _finalize_candidate(candidate, _settings(), (), frozenset())

        assert result is None

    def test_candidate_below_confidence_floor_is_dropped(self):
        candidate = _Candidate(
            category=RecommendationCategory.ANOMALY,
            kind=RecommendationKind.ACTION,
            text="Do something.",
            rationale="because",
            evidence=(
                ProvenancedClaim(value="fact", level=DataTruthLevel.DATABASE_FACT, source="x"),
            ),
            rule_name="test.LowConfidenceRule",
            confidence=0.1,
        )

        result = _finalize_candidate(
            candidate, _settings(recommendation_min_confidence=0.5), (), frozenset()
        )

        assert result is None

    def test_candidate_with_evidence_and_confidence_is_supported(self):
        candidate = _Candidate(
            category=RecommendationCategory.ANOMALY,
            kind=RecommendationKind.ACTION,
            text="Do something.",
            rationale="because",
            evidence=(
                ProvenancedClaim(value="fact", level=DataTruthLevel.DATABASE_FACT, source="x"),
            ),
            rule_name="test.SupportedRule",
            confidence=0.8,
        )

        result = _finalize_candidate(
            candidate, _settings(recommendation_min_confidence=0.5), (), frozenset()
        )

        assert result is not None
        assert result.rule_or_model == "test.SupportedRule"
        assert result.engine_version


class TestFeatureFlagAndEmptyInputs:
    def test_disabled_engine_produces_nothing(self):
        inputs = RecommendationInputs(analytics_result=_REVENUE_DECLINE)

        recs = generate_recommendations(inputs, _settings(enable_recommendation_engine=False))

        assert recs == ()

    def test_no_inputs_at_all_produces_nothing_not_a_crash(self):
        recs = generate_recommendations(RecommendationInputs(), _settings())

        assert recs == ()
