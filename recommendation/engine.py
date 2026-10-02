"""The Evidence-First Recommendation Engine -- Prompt 17
(`17_RECOMMENDATION_ENGINE_CONTRACT.md`).

`generate_recommendations` is the single entry point: it runs a fixed
pipeline -- **Data -> Finding -> Evidence -> Rule/Model -> Candidate ->
Evidence Validation -> Confidence -> Recommendation** -- over whichever
already-computed, typed evidence a caller hands it
(`RecommendationInputs`), across nine modular categories
(`recommendation.models.RecommendationCategory`): performance, anomaly,
revenue, customer, product, operations, data quality, security, and
database performance.

**Zero new queries, zero new LLM calls** on this module's own part --
every rule below consumes a typed result some other, already-existing
module already computed: `analytics.engine.compute_analytics_result`
(anomaly, data quality, revenue, customer, product), `analytics.root_cause
.investigate_root_cause` (operations), `db.query_cost.estimate_query_cost`
(database performance, per-query plan estimate), `db.query_store
.get_query_store_findings`/its cached wrapper (database performance,
SQL Server Query Store high-cost/repeated-query and regression evidence
-- Prompt 19, `19_QUERY_STORE_PERFORMANCE_CONTRACT.md`; `None` for every
non-MSSQL database), `observability.metrics.PerformanceMetrics.snapshot`
(performance), and `config.sensitive_columns.load_sensitive_columns` +
`agent.sql_validator.find_restricted_column_references` (security). This
module computes nothing from raw rows itself -- it is purely a
rules-over-already-typed-facts layer, the identical "adapt, don't
recompute" posture `analytics.provider.ResultSummaryAnalyticsProvider`
already established for its own, narrower adaptation.

**"No recommendation without evidence," enforced in code, not by
convention.** `_finalize_candidate` is the one and only place this module
ever constructs a `recommendation.models.Recommendation`, and it refuses
to do so for any candidate whose `evidence` tuple is empty -- a rule
function that found nothing to cite about a candidate simply doesn't
produce one (see each rule's own early-return shape below). This is
deliberately a **pipeline-level** invariant, not a `model_validator` on
`Recommendation` itself: `Recommendation.evidence` legally defaults to
`()`, because `analytics.forecasting.recommendations_for_forecast`'s six
pre-existing call sites construct `Recommendation`s with no `evidence` at
all and must keep validating unmodified (master rule 4 -- preserve
backward compatibility). This module's own contribution is additive: it
never emits an evidence-less recommendation, without requiring every
future producer of this type to.

**Confidence is a floor, not just a number.** A candidate whose
evidence clears the bar above but whose own rule-computed confidence
falls below `Settings.recommendation_min_confidence` is also dropped --
this is what distinguishes a "supported" recommendation (sufficient
evidence AND sufficient confidence) from an "unsupported" one (evidence
exists, but the rule itself doesn't trust it enough to surface), making
both states directly, separately testable.

**Authorization is re-checked here too, independent of whatever role the
original query ran under** -- `recommendation.engine`'s own posture on
restricted-column data, following `agent/authz.py`'s own stated principle
("no frontend-level restriction is a security boundary anywhere in this
codebase ... the corresponding internal node re-checks independently").
A candidate whose evidence touches a column `config.sensitive_columns`
classifies "restricted" is suppressed outright (never partially redacted)
unless the *viewer* of this recommendation set (`RecommendationInputs
.caller_roles` -- not necessarily the same caller who ran the original
query) holds `agent.authz.Permission.VIEW_RESTRICTED_COLUMNS`. In the one
place this is wired live today (`agent.nodes.generate_recommendations_node`,
same request, same caller), this check is provably redundant with
`agent.nodes.validate_sql_node`'s own pre-execution gate (a restricted
column could only have reached a successful execution if the running
caller already had permission) -- it exists for every *other* caller of
this engine (a future report job, a future `/recommendations` API, a
batch recompute with a different viewer's roles), where it is not
redundant at all.

**Tenant isolation**: this module has no concept of a tenant and does not
need one -- it operates entirely on data already scoped to one resolved
database connection/one request (the same `AgentState["selected_database"]`
scoping every other node in this graph already respects), computes
nothing new from the database itself, and persists nothing anywhere. It
is a pure function of its typed inputs. This is a materially narrower
surface than `onboarding/`/`semantic/catalog.py` (which do carry a real
`tenant_id` because they persist cross-request, cross-user state) -- if a
future prompt persists recommendations for later, cross-viewer retrieval,
*that* storage layer would need the identical `tenant_id` + policy-module
pattern those two already establish; this module's own job stops at
producing the typed value for one request.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from analytics.models import (
    AnalyticsFindingKind,
    AnalyticsResult,
    RootCauseResult,
)
from pydantic import BaseModel, ConfigDict

from agent.authz import Permission, has_role_permission
from agent.provenance import DataTruthLevel, ProvenancedClaim
from config.sensitive_columns import load_sensitive_columns
from config.settings import Settings, get_settings
from db.query_cost import CostEstimate
from db.query_store import QueryStoreFindings
from recommendation.models import Recommendation, RecommendationCategory, RecommendationKind
from security.audit_log import log_security_event

logger = logging.getLogger(__name__)

# Column-name keyword heuristics -- a disclosed, bounded approach (no
# business-glossary lookup is attempted here), the identical posture
# `analytics.visualization.detect_geography_columns`/`agent.complexity`'s
# own regex signals already establish elsewhere in this codebase: a
# reasonable, inspectable proxy, not a claim of semantic certainty. Every
# candidate these keyword-gated rules produce discloses this in its own
# `limitations`.
_REVENUE_KEYWORDS: tuple[str, ...] = (
    "revenue",
    "sales",
    "amount",
    "price",
    "income",
    "profit",
    "earnings",
    "total",
)
_CUSTOMER_KEYWORDS: tuple[str, ...] = (
    "customer",
    "client",
    "account",
    "subscriber",
    "member",
    "buyer",
)
_PRODUCT_KEYWORDS: tuple[str, ...] = (
    "product",
    "sku",
    "item",
    "category",
    "inventory",
    "stock",
)


class RecommendationInputs(BaseModel):
    """Everything `generate_recommendations` can draw evidence from --
    every field optional, since any one caller will typically have only a
    subset available (a report job may have no live `performance_snapshot`;
    the live graph node never has a `root_cause_result`, see this module's
    own docstring).

    Attributes:
        analytics_result: `analytics.engine.compute_analytics_result`'s
            output for the query result under consideration -- feeds the
            ANOMALY, DATA_QUALITY, REVENUE, CUSTOMER, and PRODUCT rules.
        root_cause_result: `analytics.root_cause.investigate_root_cause`'s
            output, when a caller has one (two datasets -- see that
            module's own disclosed "not wired into any live node" gap,
            which this module inherits rather than works around) --
            feeds the OPERATIONS rule.
        cost_estimate: `db.query_cost.estimate_query_cost`'s output for
            the executed query -- feeds the DATABASE_PERFORMANCE rule.
        query_store_findings: `db.query_store.get_query_store_findings`
            (or its cached wrapper)'s output -- SQL Server Query Store
            high-cost/repeated-query and regression evidence, also
            feeding the DATABASE_PERFORMANCE rule (Prompt 19,
            `19_QUERY_STORE_PERFORMANCE_CONTRACT.md`). `None` for every
            non-MSSQL database, or when Query Store itself is
            unavailable -- see that module's own docstring; this field
            being `None` produces zero DATABASE_PERFORMANCE candidates
            from this source, never an error.
        performance_snapshot: `observability.metrics.PerformanceMetrics
            .snapshot()`'s output -- feeds the PERFORMANCE rule. A plain
            mapping (not the `MetricsSnapshot` TypedDict directly) so a
            caller can pass either the live snapshot or a plain dict in
            a test without importing that module's own internal type.
        restricted_column_hits: `(table_name, column_name)` pairs already
            found to be both classified "restricted"
            (`config.sensitive_columns`) and referenced by the query this
            recommendation set is about -- the caller computes this (via
            `agent.sql_validator.find_restricted_column_references`), not
            this module, to avoid a second SQL-parsing implementation.
            Feeds the SECURITY rule.
        caller_roles: The roles of whoever will actually *view* this
            recommendation set -- used only for the authorization check
            described in this module's own docstring. Defaults to `()`
            (no permissions), fail-closed like every other role-sequence
            default in this codebase (`agent.authz.has_role_permission`).
    """

    model_config = ConfigDict(frozen=True)

    analytics_result: AnalyticsResult | None = None
    root_cause_result: RootCauseResult | None = None
    cost_estimate: CostEstimate | None = None
    query_store_findings: QueryStoreFindings | None = None
    performance_snapshot: Mapping[str, Any] | None = None
    restricted_column_hits: tuple[tuple[str, str], ...] = ()
    caller_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    """One rule's own proposal, before evidence/confidence/authorization
    validation -- never exposed outside this module (`Recommendation` is
    the public, typed equivalent `_finalize_candidate` constructs from
    this, mirroring `analytics.forecasting._FittedModel`'s identical
    "internal working shape vs. public typed result" split).
    """

    category: RecommendationCategory
    kind: RecommendationKind
    text: str
    rationale: str
    evidence: tuple[ProvenancedClaim, ...]
    rule_name: str
    affected_entity: str | None = None
    action: str | None = None
    measurable_impact: str | None = None
    confidence: float | None = None
    limitations: tuple[str, ...] = ()
    #: Column name(s) this candidate's evidence is about, for the
    #: authorization check only -- deliberately just names, not
    #: `(table, column)` pairs, matching `agent.sql_validator
    #: .find_restricted_column_references`'s own disclosed "name-based,
    #: conservative in the safe direction" matching convention: a false
    #: positive here only costs an unnecessarily-suppressed recommendation,
    #: while a false negative would let restricted data through
    #: unauthorized.
    evidence_columns: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Rules -- one function per category (ANOMALY/DATA_QUALITY/OPERATIONS/
# DATABASE_PERFORMANCE/PERFORMANCE/SECURITY are each their own category;
# REVENUE/CUSTOMER/PRODUCT share two generic rule shapes, parameterized by
# keyword list + category + rule name, rather than three near-duplicate
# functions).
# ---------------------------------------------------------------------------


def _anomaly_candidates(result: AnalyticsResult) -> list[_Candidate]:
    """ANOMALY: one candidate per `analytics.anomaly.detect_anomalies`
    flag already folded into `result.findings` by `analytics.engine
    .compute_analytics_result`'s own TIME_SERIES branch -- zero new
    computation, this rule only renders what's already there.
    """
    candidates: list[_Candidate] = []
    for finding in result.findings:
        if finding.kind != AnalyticsFindingKind.ANOMALY or finding.anomaly is None:
            continue
        anomaly = finding.anomaly
        methods = sorted({signal.method.value for signal in anomaly.signals})
        # More independent methods agreeing -> higher confidence. A
        # deterministic, disclosed formula (not a statistically derived
        # one), the identical honesty precedent `analytics.forecasting
        # ._HIGH_MAPE_PERCENT`'s own round-number threshold already sets.
        confidence = min(1.0, round(0.5 + 0.15 * len(anomaly.signals), 2))
        action = f"Investigate the value at {anomaly.period} ({anomaly.value:g})."
        candidates.append(
            _Candidate(
                category=RecommendationCategory.ANOMALY,
                kind=RecommendationKind.ACTION,
                text=(
                    f"The value at {anomaly.period} ({anomaly.value:g}) was flagged as "
                    f"anomalous by: {', '.join(methods)}. {action}"
                ),
                rationale="analytics.anomaly.detect_anomalies flagged this point.",
                evidence=(finding.claim,),
                affected_entity=anomaly.period,
                action=action,
                measurable_impact=f"{len(anomaly.signals)} detection method(s) agree on this point.",
                confidence=confidence,
                rule_name="recommendation.engine.AnomalyRule",
                limitations=(
                    "Anomaly detection is a statistical signal, not a confirmed cause -- see "
                    "analytics.root_cause for contribution analysis once a comparison baseline "
                    "period is available.",
                ),
            )
        )
    return candidates


def _data_quality_candidates(result: AnalyticsResult, settings: Settings) -> list[_Candidate]:
    """DATA_QUALITY: a column whose null rate, within this one result,
    clears `Settings.recommendation_null_rate_threshold`."""
    candidates: list[_Candidate] = []
    for finding in result.findings:
        if finding.kind != AnalyticsFindingKind.COLUMN_SUMMARY or finding.column_summary is None:
            continue
        stat = finding.column_summary
        if stat.count <= 0:
            continue
        null_rate = stat.null_count / stat.count
        if null_rate < settings.recommendation_null_rate_threshold:
            continue
        confidence = min(1.0, round(0.5 + null_rate, 2))
        action = f"Investigate data completeness for column '{stat.column}'."
        candidates.append(
            _Candidate(
                category=RecommendationCategory.DATA_QUALITY,
                kind=RecommendationKind.ACTION,
                text=(
                    f"Column '{stat.column}' is {null_rate:.1%} null ({stat.null_count} of "
                    f"{stat.count} value(s) in this result). {action}"
                ),
                rationale="High null rate detected in the executed result's own column summary.",
                evidence=(finding.claim,),
                affected_entity=stat.column,
                action=action,
                measurable_impact=(
                    f"{null_rate:.1%} of this result's rows affected "
                    f"({stat.null_count} of {stat.count})."
                ),
                confidence=confidence,
                rule_name="recommendation.engine.HighNullRateRule",
                limitations=(
                    "Based on this one result's own rows, not a full table scan -- a "
                    "differently-filtered or larger query may show a different rate.",
                ),
                evidence_columns=(stat.column,),
            )
        )
    return candidates


def _declining_metric_candidates(
    result: AnalyticsResult,
    category: RecommendationCategory,
    keywords: tuple[str, ...],
    rule_name: str,
) -> list[_Candidate]:
    """REVENUE (and, for a product-named metric, PRODUCT): a `GROWTH`
    finding classified "down" whose value column's name matches one of
    `keywords`."""
    candidates: list[_Candidate] = []
    for finding in result.findings:
        if finding.kind != AnalyticsFindingKind.GROWTH or finding.growth is None:
            continue
        growth = finding.growth
        if growth.direction != "down" or growth.overall_change_percent is None:
            continue
        if not any(keyword in growth.value_column.lower() for keyword in keywords):
            continue
        magnitude = abs(growth.overall_change_percent)
        confidence = min(1.0, round(0.4 + magnitude / 100, 2))
        action = f"Review the drivers behind the decline in '{growth.value_column}'."
        candidates.append(
            _Candidate(
                category=category,
                kind=RecommendationKind.ACTION,
                text=(
                    f"'{growth.value_column}' fell {magnitude:.1f}% from "
                    f"{growth.points[0].period} to {growth.points[-1].period}. {action}"
                ),
                rationale=(
                    f"analytics.engine's own growth finding classified "
                    f"{growth.value_column!r} as declining."
                ),
                evidence=(finding.claim,),
                affected_entity=growth.value_column,
                action=action,
                measurable_impact=f"{magnitude:.1f}% decline over the observed period.",
                confidence=confidence,
                rule_name=rule_name,
                limitations=(
                    f"Column-name keyword match ({', '.join(keywords)}) is a heuristic, not a "
                    "confirmed business classification -- verify this column's actual business "
                    "meaning before acting.",
                ),
                evidence_columns=(growth.value_column,),
            )
        )
    return candidates


def _concentration_candidates(
    result: AnalyticsResult,
    category: RecommendationCategory,
    keywords: tuple[str, ...],
    rule_name: str,
    settings: Settings,
) -> list[_Candidate]:
    """CUSTOMER/PRODUCT: a `RANKING` finding whose top entry's share of
    the total clears `Settings.recommendation_concentration_share_threshold`,
    on a label/value column matching one of `keywords` -- a concentration/
    dependency-risk signal (e.g. one customer or product dominating the
    total)."""
    candidates: list[_Candidate] = []
    for finding in result.findings:
        if finding.kind != AnalyticsFindingKind.RANKING or finding.ranking is None:
            continue
        ranking = finding.ranking
        if not ranking.entries:
            continue
        top = ranking.entries[0]
        if top.share_percent is None:
            continue
        if top.share_percent < settings.recommendation_concentration_share_threshold:
            continue
        haystack = f"{ranking.label_column} {ranking.value_column}".lower()
        if not any(keyword in haystack for keyword in keywords):
            continue
        confidence = min(1.0, round(top.share_percent / 100, 2))
        action = (
            f"Review concentration risk: '{top.label}' accounts for "
            f"{top.share_percent:.1f}% of total {ranking.value_column}."
        )
        candidates.append(
            _Candidate(
                category=category,
                kind=RecommendationKind.ACTION,
                text=action,
                rationale=(
                    "analytics.engine's own ranking finding shows a single label dominating "
                    "the total."
                ),
                evidence=(finding.claim,),
                affected_entity=top.label,
                action=action,
                measurable_impact=f"{top.share_percent:.1f}% of total {ranking.value_column}.",
                confidence=confidence,
                rule_name=rule_name,
                limitations=(
                    f"Column-name keyword match ({', '.join(keywords)}) is a heuristic, not a "
                    "confirmed business classification -- verify this column's actual business "
                    "meaning before acting.",
                ),
                evidence_columns=(ranking.value_column,),
            )
        )
    return candidates


def _operations_candidates(root_cause: RootCauseResult) -> list[_Candidate]:
    """OPERATIONS: the top validated contributor from an already-computed
    `analytics.root_cause.investigate_root_cause` result -- never fires
    when `has_sufficient_evidence` is `False` (that function's own
    structural guarantee that `contributors`/`confidence` stay empty/`None`
    otherwise, see its own docstring)."""
    if not root_cause.has_sufficient_evidence or not root_cause.contributors:
        return []
    top = root_cause.contributors[0]
    direction = "increase" if top.contribution > 0 else "decrease"
    action = (
        f"Investigate '{top.label}', which explains "
        f"{abs(top.contribution_percent):.1f}% of the change."
    )
    evidence_claim = ProvenancedClaim(
        value=(
            f"'{top.label}' moved from {top.baseline_value:g} to {top.current_value:g} "
            f"({top.contribution_percent:+.1f}% of the total change)."
        ),
        level=DataTruthLevel.DATABASE_FACT,
        source="analytics.root_cause.investigate_root_cause",
    )
    return [
        _Candidate(
            category=RecommendationCategory.OPERATIONS,
            kind=RecommendationKind.ACTION,
            text=(
                f"The largest contributor to this {direction} is '{top.label}' "
                f"({abs(top.contribution_percent):.1f}% of the total change). {action}"
            ),
            rationale="analytics.root_cause.investigate_root_cause identified a validated contributor.",
            evidence=(evidence_claim,),
            affected_entity=top.label,
            action=action,
            measurable_impact=(
                f"{abs(top.contribution_percent):.1f}% of the total change "
                f"({root_cause.confidence:.0%} confidence)."
                if root_cause.confidence is not None
                else None
            ),
            confidence=root_cause.confidence,
            rule_name="recommendation.engine.OperationsRootCauseRule",
            limitations=root_cause.limitations,
        )
    ]


def _database_performance_candidates(estimate: CostEstimate) -> list[_Candidate]:
    """DATABASE_PERFORMANCE: a `db.query_cost.estimate_query_cost` plan
    estimate classified "moderate" or "high" severity."""
    if estimate.severity == "low":
        return []
    confidence = 0.9 if estimate.severity == "high" else 0.6
    rows_text = (
        f"~{int(estimate.estimated_rows):,} rows"
        if estimate.estimated_rows
        else "a large amount of data"
    )
    action = "Review the query for a missing filter or index, or narrow the scanned date range."
    evidence_claim = ProvenancedClaim(
        value=(
            f"The query's execution plan estimates {rows_text} scanned via "
            f"{estimate.plan_summary!r} (severity={estimate.severity})."
        ),
        level=DataTruthLevel.DATABASE_FACT,
        source="db.query_cost.estimate_query_cost",
    )
    return [
        _Candidate(
            category=RecommendationCategory.DATABASE_PERFORMANCE,
            kind=RecommendationKind.ACTION,
            text=(
                f"This query's execution plan estimates {rows_text} scanned "
                f"(severity={estimate.severity}). {action}"
            ),
            rationale="db.query_cost.estimate_query_cost's non-executing plan flagged this query.",
            evidence=(evidence_claim,),
            affected_entity=estimate.plan_summary,
            action=action,
            measurable_impact=rows_text,
            confidence=confidence,
            rule_name="recommendation.engine.HighCostQueryRule",
            limitations=(
                "A planner estimate, not a measured execution -- real cost may differ, and "
                "cost/row units are never comparable across database engines (see "
                "db.query_cost's own docstring).",
            ),
        )
    ]


def _query_store_high_cost_candidates(
    findings: QueryStoreFindings, settings: Settings
) -> list[_Candidate]:
    """DATABASE_PERFORMANCE: a SQL Server Query Store query whose average
    duration clears `Settings.query_store_high_duration_ms_threshold` --
    "high-cost/repeated" by construction, since `db.query_store
    .get_top_queries` already filtered to queries clearing
    `Settings.query_store_min_execution_count` over the lookback window
    (Prompt 19, `19_QUERY_STORE_PERFORMANCE_CONTRACT.md`).

    Evidence is built entirely from already literal-masked/numeric
    fields -- `query.normalized_sql_preview` never contains a literal
    value (see `db.query_store._mask_literals`'s own docstring), and
    `affected_entity` is the query's hash fingerprint, never its text.
    """
    candidates: list[_Candidate] = []
    for query in findings.top_queries:
        if query.avg_duration_ms < settings.query_store_high_duration_ms_threshold:
            continue
        confidence = min(
            1.0,
            round(
                0.5
                + (query.avg_duration_ms / settings.query_store_high_duration_ms_threshold) * 0.1,
                2,
            ),
        )
        action = (
            "Review this query's execution plan for a missing index, stale statistics, "
            "or a parameter-sniffing issue."
        )
        evidence_claim = ProvenancedClaim(
            value=(
                f"Query {query.query_fingerprint} ({query.normalized_sql_preview}) executed "
                f"{query.execution_count} time(s) over the last {findings.lookback_hours:g}h, "
                f"averaging {query.avg_duration_ms:.0f}ms, {query.avg_cpu_ms:.0f}ms CPU, "
                f"{query.avg_logical_reads:.0f} logical reads."
            ),
            level=DataTruthLevel.DATABASE_FACT,
            source="db.query_store.get_top_queries",
        )
        candidates.append(
            _Candidate(
                category=RecommendationCategory.DATABASE_PERFORMANCE,
                kind=RecommendationKind.ACTION,
                text=(
                    f"Query {query.query_fingerprint} is a high-cost, repeated pattern "
                    f"({query.execution_count} execution(s), averaging "
                    f"{query.avg_duration_ms:.0f}ms). {action}"
                ),
                rationale="SQL Server Query Store flagged this query across the lookback window.",
                evidence=(evidence_claim,),
                affected_entity=query.query_fingerprint,
                action=action,
                measurable_impact=(
                    f"{query.execution_count} execution(s), avg {query.avg_duration_ms:.0f}ms, "
                    f"{query.plan_count} plan(s) recorded."
                ),
                confidence=confidence,
                rule_name="recommendation.engine.QueryStoreHighCostRepeatedQueryRule",
                limitations=(
                    "Query Store data reflects historical server-wide activity, not "
                    "specifically this question -- this query may or may not be the one "
                    "just executed.",
                    "Query text literals are masked; only a normalized preview is shown.",
                ),
            )
        )
    return candidates


def _query_store_regression_candidates(findings: QueryStoreFindings) -> list[_Candidate]:
    """DATABASE_PERFORMANCE: a SQL Server Query Store regression --
    `db.query_store.get_regressions` already validated that the recent
    window's execution count, the baseline window's execution count, and
    the regression factor itself all clear their configured bars; this
    rule only renders what's already there, the identical "adapt, don't
    recompute" posture every other rule in this module follows."""
    candidates: list[_Candidate] = []
    for regression in findings.regressions:
        confidence = min(1.0, round(0.4 + regression.regression_factor * 0.1, 2))
        action = (
            "Investigate what changed for this query -- a plan change, stale statistics, "
            "a data-volume shift, or a recent schema/index change are the usual causes."
        )
        evidence_claim = ProvenancedClaim(
            value=(
                f"Query {regression.query_fingerprint} ({regression.normalized_sql_preview}) "
                f"regressed: {regression.recent_avg_duration_ms:.0f}ms average over "
                f"{regression.recent_execution_count} recent execution(s), vs. a "
                f"{regression.baseline_avg_duration_ms:.0f}ms baseline over "
                f"{regression.baseline_execution_count} execution(s) -- "
                f"{regression.regression_factor:.1f}x slower."
            ),
            level=DataTruthLevel.DATABASE_FACT,
            source="db.query_store.get_regressions",
        )
        candidates.append(
            _Candidate(
                category=RecommendationCategory.DATABASE_PERFORMANCE,
                kind=RecommendationKind.ACTION,
                text=(
                    f"Query {regression.query_fingerprint} has regressed "
                    f"{regression.regression_factor:.1f}x against its own recent baseline. {action}"
                ),
                rationale="SQL Server Query Store's own recent-vs-baseline comparison flagged this query.",
                evidence=(evidence_claim,),
                affected_entity=regression.query_fingerprint,
                action=action,
                measurable_impact=(
                    f"{regression.regression_factor:.1f}x slower "
                    f"({regression.recent_avg_duration_ms:.0f}ms vs. "
                    f"{regression.baseline_avg_duration_ms:.0f}ms baseline)."
                ),
                confidence=confidence,
                rule_name="recommendation.engine.QueryStoreRegressionRule",
                limitations=(
                    "A statistical comparison of this query against its own recent "
                    "history, not a root-cause diagnosis of why it changed.",
                    "Query text literals are masked; only a normalized preview is shown.",
                ),
            )
        )
    return candidates


def _performance_candidates(snapshot: Mapping[str, Any], settings: Settings) -> list[_Candidate]:
    """PERFORMANCE: a LangGraph pipeline stage whose rolling p95 duration
    (`observability.metrics.PerformanceMetrics.snapshot()`) clears
    `Settings.recommendation_slow_stage_ms_threshold`."""
    candidates: list[_Candidate] = []
    for stage in snapshot.get("stages") or []:
        p95 = stage.get("p95_ms")
        if p95 is None or p95 < settings.recommendation_slow_stage_ms_threshold:
            continue
        confidence = min(1.0, round(p95 / (settings.recommendation_slow_stage_ms_threshold * 3), 2))
        action = (
            f"Investigate the '{stage['stage']}' pipeline stage for optimization opportunities."
        )
        evidence_claim = ProvenancedClaim(
            value=(
                f"Stage '{stage['stage']}' has a p95 duration of {p95:.0f}ms across "
                f"{stage['count']} recent request(s) (mean {stage['mean_ms']:.0f}ms)."
            ),
            level=DataTruthLevel.DATABASE_FACT,
            source="observability.metrics.PerformanceMetrics.snapshot",
        )
        candidates.append(
            _Candidate(
                category=RecommendationCategory.PERFORMANCE,
                kind=RecommendationKind.ACTION,
                text=(
                    f"The '{stage['stage']}' stage is slow (p95 {p95:.0f}ms over "
                    f"{stage['count']} recent request(s)). {action}"
                ),
                rationale="observability.metrics.PerformanceMetrics' live rollup flagged this stage.",
                evidence=(evidence_claim,),
                affected_entity=stage["stage"],
                action=action,
                measurable_impact=(
                    f"p95={p95:.0f}ms, mean={stage['mean_ms']:.0f}ms over {stage['count']} "
                    "recent request(s)."
                ),
                confidence=confidence,
                rule_name="recommendation.engine.SlowStageRule",
                limitations=(
                    "Single-process, in-memory rollup -- see observability.metrics' own "
                    "disclosed multi-worker/resets-on-restart limitation.",
                ),
            )
        )
    return candidates


def _security_candidates(restricted_hits: tuple[tuple[str, str], ...]) -> list[_Candidate]:
    """SECURITY: one candidate per already-detected (table, column) pair
    classified "restricted" (`config.sensitive_columns`) that this
    result's own query referenced. Evidence is `CONFIRMED_BUSINESS_TRUTH`
    (the classification itself is a hand-reviewed precedent, per
    `agent.provenance.DataTruthLevel`'s own docstring), not
    `DATABASE_FACT` -- the two different kinds of ground truth this
    module's evidence validator accepts (see this module's own docstring).
    """
    candidates: list[_Candidate] = []
    for table_name, column_name in restricted_hits:
        action = (
            f"Confirm that access to '{table_name}.{column_name}' (classified restricted) "
            "is expected and authorized."
        )
        evidence_claim = ProvenancedClaim(
            value=(
                f"'{table_name}.{column_name}' is classified 'restricted' in "
                "config/sensitive_columns.yaml and was referenced by this query's result."
            ),
            level=DataTruthLevel.CONFIRMED_BUSINESS_TRUTH,
            source="config.sensitive_columns.load_sensitive_columns",
        )
        candidates.append(
            _Candidate(
                category=RecommendationCategory.SECURITY,
                kind=RecommendationKind.ACTION,
                text=(
                    f"This result includes '{table_name}.{column_name}', a column classified "
                    f"restricted. {action}"
                ),
                rationale=(
                    "config.sensitive_columns' hand-reviewed classification matched a column "
                    "in this result."
                ),
                evidence=(evidence_claim,),
                affected_entity=f"{table_name}.{column_name}",
                action=action,
                measurable_impact=None,
                confidence=1.0,
                rule_name="recommendation.engine.RestrictedColumnExposureRule",
                limitations=(
                    "Detection is name-based (matching agent.sql_validator's own conservative "
                    "matching convention), not full table-qualification resolution.",
                ),
                evidence_columns=(column_name,),
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Evidence validation, confidence floor, authorization, construction.
# ---------------------------------------------------------------------------


def _restricted_column_names(settings: Settings) -> frozenset[str]:
    """Every column name (lowercased, table-agnostic) classified
    "restricted" anywhere in `config/sensitive_columns.yaml` -- loaded
    once per `generate_recommendations` call (that loader's own docstring
    says it's a small, uncached-by-design file read, cheap to call once
    per request, matching every other live call site's own convention).
    """
    del settings  # reserved for a future per-database override; unused today
    classifications = load_sensitive_columns()
    return frozenset(
        column.lower() for (_table, column), tier in classifications.items() if tier == "restricted"
    )


def _authorized(
    candidate: _Candidate, caller_roles: tuple[str, ...], restricted_column_names: frozenset[str]
) -> bool:
    """True unless `candidate`'s evidence touches a column classified
    restricted and `caller_roles` lacks `Permission.VIEW_RESTRICTED_COLUMNS`
    -- see this module's own docstring for the full reasoning."""
    if not candidate.evidence_columns or not restricted_column_names:
        return True
    touches_restricted = any(
        column.lower() in restricted_column_names for column in candidate.evidence_columns
    )
    if not touches_restricted:
        return True
    return has_role_permission(caller_roles, Permission.VIEW_RESTRICTED_COLUMNS)


def _finalize_candidate(
    candidate: _Candidate,
    settings: Settings,
    caller_roles: tuple[str, ...],
    restricted_column_names: frozenset[str],
) -> Recommendation | None:
    """The one and only construction point for a `Recommendation` this
    module produces -- see this module's own docstring for why "no
    recommendation without evidence" and the confidence floor are both
    enforced here, pipeline-side, rather than as a `Recommendation`
    `model_validator`.
    """
    if not candidate.evidence:
        logger.debug("[recommendation] dropping %s candidate: no evidence", candidate.rule_name)
        return None
    if (
        candidate.confidence is None
        or candidate.confidence < settings.recommendation_min_confidence
    ):
        logger.debug(
            "[recommendation] dropping %s candidate: confidence %s below floor %.2f",
            candidate.rule_name,
            candidate.confidence,
            settings.recommendation_min_confidence,
        )
        return None
    if not _authorized(candidate, caller_roles, restricted_column_names):
        log_security_event(
            "recommendation_evidence_suppressed",
            "info",
            "A recommendation referencing a restricted column was suppressed for a "
            "viewer lacking VIEW_RESTRICTED_COLUMNS.",
            rule=candidate.rule_name,
            columns=candidate.evidence_columns,
        )
        return None

    return Recommendation(
        kind=candidate.kind,
        claim=ProvenancedClaim(
            value=candidate.text,
            level=DataTruthLevel.AI_INFERENCE,
            source=candidate.rule_name,
            grounded_in=tuple(item.value for item in candidate.evidence),
        ),
        rationale=candidate.rationale,
        category=candidate.category,
        evidence=candidate.evidence,
        affected_entity=candidate.affected_entity,
        action=candidate.action,
        measurable_impact=candidate.measurable_impact,
        confidence=candidate.confidence,
        rule_or_model=candidate.rule_name,
        limitations=candidate.limitations,
    )


def generate_recommendations(
    inputs: RecommendationInputs, settings: Settings | None = None
) -> tuple[Recommendation, ...]:
    """Runs every category's rule(s) over whichever typed evidence
    `inputs` actually supplies, then validates, scores, and authorizes
    each resulting candidate before constructing a `Recommendation`.

    Args:
        inputs: See `RecommendationInputs`'s own docstring -- every field
            is optional; a rule whose required input is `None`/empty
            simply contributes no candidates (never an error).
        settings: Defaults to `config.settings.get_settings()`.

    Returns:
        Every recommendation that cleared evidence validation, the
        confidence floor, and authorization, in deterministic rule order
        (never shuffled/sorted by score) -- empty, never `None`, matching
        this codebase's "always iterable" convention for plural result
        fields.
    """
    settings = settings or get_settings()
    if not settings.enable_recommendation_engine:
        return ()

    candidates: list[_Candidate] = []

    if inputs.analytics_result is not None:
        candidates.extend(_anomaly_candidates(inputs.analytics_result))
        candidates.extend(_data_quality_candidates(inputs.analytics_result, settings))
        candidates.extend(
            _declining_metric_candidates(
                inputs.analytics_result,
                RecommendationCategory.REVENUE,
                _REVENUE_KEYWORDS,
                "recommendation.engine.DecliningRevenueRule",
            )
        )
        candidates.extend(
            _concentration_candidates(
                inputs.analytics_result,
                RecommendationCategory.CUSTOMER,
                _CUSTOMER_KEYWORDS,
                "recommendation.engine.CustomerConcentrationRule",
                settings,
            )
        )
        candidates.extend(
            _concentration_candidates(
                inputs.analytics_result,
                RecommendationCategory.PRODUCT,
                _PRODUCT_KEYWORDS,
                "recommendation.engine.ProductConcentrationRule",
                settings,
            )
        )

    if inputs.root_cause_result is not None:
        candidates.extend(_operations_candidates(inputs.root_cause_result))

    if inputs.cost_estimate is not None:
        candidates.extend(_database_performance_candidates(inputs.cost_estimate))

    if (
        inputs.query_store_findings is not None
        and inputs.query_store_findings.availability.available
    ):
        candidates.extend(_query_store_high_cost_candidates(inputs.query_store_findings, settings))
        candidates.extend(_query_store_regression_candidates(inputs.query_store_findings))

    if inputs.performance_snapshot is not None:
        candidates.extend(_performance_candidates(inputs.performance_snapshot, settings))

    if inputs.restricted_column_hits:
        candidates.extend(_security_candidates(inputs.restricted_column_hits))

    restricted_column_names = _restricted_column_names(settings)
    recommendations = [
        rec
        for candidate in candidates
        if (
            rec := _finalize_candidate(
                candidate, settings, inputs.caller_roles, restricted_column_names
            )
        )
        is not None
    ]
    return tuple(recommendations)
