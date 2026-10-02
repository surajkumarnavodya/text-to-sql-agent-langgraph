"""Analytics cases: the deterministic analytics engine
(`analytics.engine.compute_analytics_result`).

Each case feeds realistic `(columns, rows)` shapes (the same shape a real
SQL execution would produce) and asserts the engine classifies the
correct `ResultShape` and produces the finding a human would expect --
"did result-shape classification and the headline finding stay correct,"
not a re-run of `tests/test_analytics_*.py`'s own much finer-grained unit
coverage of every formula.
"""

from __future__ import annotations

from analytics.engine import compute_analytics_result
from analytics.models import AnalyticsFindingKind, ResultShape

from eval.component_benchmark.schema import ComponentCase


def _case_scalar_shape_classified_correctly() -> tuple[bool, str]:
    result = compute_analytics_result(["total_revenue"], [(150_000.0,)])
    passed = result.shape == ResultShape.SCALAR
    return passed, f"shape={result.shape}"


def _case_time_series_detects_upward_growth() -> tuple[bool, str]:
    result = compute_analytics_result(
        ["year", "revenue"],
        [("2021", 100_000.0), ("2022", 120_000.0), ("2023", 150_000.0)],
    )
    growth_findings = [f for f in result.findings if f.kind == AnalyticsFindingKind.GROWTH]
    passed = result.shape == ResultShape.TIME_SERIES and len(growth_findings) >= 1
    return passed, f"shape={result.shape} growth_findings={len(growth_findings)}"


def _case_time_series_detects_downward_growth() -> tuple[bool, str]:
    result = compute_analytics_result(
        ["year", "revenue"],
        [("2021", 150_000.0), ("2022", 120_000.0), ("2023", 90_000.0)],
    )
    growth_findings = [f for f in result.findings if f.kind == AnalyticsFindingKind.GROWTH]
    claim_texts = [f.claim.value.lower() for f in growth_findings]
    direction_ok = any(
        "-" in text or "declin" in text or "fell" in text or "down" in text for text in claim_texts
    )
    passed = bool(result.shape == ResultShape.TIME_SERIES and growth_findings and direction_ok)
    return passed, f"shape={result.shape} claims={claim_texts}"


def _case_categorical_aggregate_produces_ranking() -> tuple[bool, str]:
    result = compute_analytics_result(
        ["region", "revenue"],
        [("East", 500_000.0), ("West", 300_000.0), ("North", 100_000.0)],
    )
    ranking_findings = [f for f in result.findings if f.kind == AnalyticsFindingKind.RANKING]
    passed = result.shape == ResultShape.CATEGORICAL_AGGREGATE and len(ranking_findings) >= 1
    return passed, f"shape={result.shape} ranking_findings={len(ranking_findings)}"


def _case_empty_result_classified_as_empty() -> tuple[bool, str]:
    result = compute_analytics_result(["region", "revenue"], [])
    passed = result.shape == ResultShape.EMPTY and result.row_count == 0
    return passed, f"shape={result.shape} row_count={result.row_count}"


def _case_detail_listing_with_no_numeric_measure_classified_as_raw_table() -> tuple[bool, str]:
    """No numeric 'value' column at all (a row-detail listing, not an
    aggregate) -- classify_result_shape's own RAW_TABLE fallback when it
    can't find a value column to rank/trend."""
    result = compute_analytics_result(
        ["name", "email", "region", "status"],
        [
            ("Alice", "a@x.com", "East", "active"),
            ("Bob", "b@x.com", "West", "active"),
            ("Cara", "c@x.com", "North", "inactive"),
        ],
    )
    passed = result.shape == ResultShape.RAW_TABLE
    return passed, f"shape={result.shape}"


def _case_engine_version_always_stamped() -> tuple[bool, str]:
    """Every result carries its own engine version -- the literal
    "record formula/version metadata" requirement -- never blank."""
    result = compute_analytics_result(["total_revenue"], [(1.0,)])
    passed = bool(result.engine_version)
    return passed, f"engine_version={result.engine_version!r}"


#: Only the Prompt 13 statistical sub-objects document their own
#: `formula: str` (the literal "record formula/version metadata"
#: requirement) -- `ColumnSummaryStat`/`DistinctCountStat` are simple
#: descriptive stats with no such field, and `AnomalyPoint`'s own formula
#: lives one level deeper, per flagged `AnomalySignal` (Prompt 14), not on
#: the point itself. This case is scoped to the kinds that actually make
#: the "formula" promise, not every kind that happens to appear.
_FORMULA_BEARING_FINDING_ATTR: dict[AnalyticsFindingKind, str] = {
    AnalyticsFindingKind.VARIANCE: "variance",
    AnalyticsFindingKind.MEDIAN: "median",
    AnalyticsFindingKind.PERCENTILE: "percentile",
    AnalyticsFindingKind.GROWTH: "growth",
    AnalyticsFindingKind.RANKING: "ranking",
}


def _case_every_formula_bearing_finding_carries_its_own_formula() -> tuple[bool, str]:
    result = compute_analytics_result(
        ["year", "revenue"],
        [("2021", 100.0), ("2022", 120.0), ("2023", 150.0)],
    )
    checked = [f for f in result.findings if f.kind in _FORMULA_BEARING_FINDING_ATTR]
    missing_formula = [
        f.kind
        for f in checked
        if not getattr(getattr(f, _FORMULA_BEARING_FINDING_ATTR[f.kind]), "formula", None)
    ]
    passed = len(checked) > 0 and not missing_formula
    return passed, f"checked={len(checked)} missing_formula={missing_formula}"


CASES: list[ComponentCase] = [
    ComponentCase(
        "scalar_shape_classified_correctly",
        "A single-row, single-column result is classified SCALAR.",
        _case_scalar_shape_classified_correctly,
    ),
    ComponentCase(
        "time_series_detects_upward_growth",
        "A rising year-over-year revenue series produces a GROWTH finding.",
        _case_time_series_detects_upward_growth,
    ),
    ComponentCase(
        "time_series_detects_downward_growth",
        "A falling year-over-year revenue series produces a GROWTH finding describing a decline.",
        _case_time_series_detects_downward_growth,
    ),
    ComponentCase(
        "categorical_aggregate_produces_ranking",
        "A category-by-revenue breakdown is classified CATEGORICAL_AGGREGATE with a RANKING finding.",
        _case_categorical_aggregate_produces_ranking,
    ),
    ComponentCase(
        "empty_result_classified_as_empty",
        "Zero rows is classified EMPTY with row_count 0, never an error.",
        _case_empty_result_classified_as_empty,
    ),
    ComponentCase(
        "detail_listing_with_no_numeric_measure_classified_as_raw_table",
        "A row-detail listing with no numeric measure column is classified RAW_TABLE.",
        _case_detail_listing_with_no_numeric_measure_classified_as_raw_table,
    ),
    ComponentCase(
        "engine_version_always_stamped",
        "Every AnalyticsResult carries a non-empty engine_version.",
        _case_engine_version_always_stamped,
    ),
    ComponentCase(
        "every_formula_bearing_finding_carries_its_own_formula",
        "Every formula-documenting finding kind (variance/median/percentile/growth/ranking) carries a non-empty formula.",
        _case_every_formula_bearing_finding_carries_its_own_formula,
    ),
]
