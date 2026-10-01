"""Typed shapes for one query result's analytics findings.

Every `AnalyticsFinding` this module's own default provider produces
carries `claim.level == DataTruthLevel.DATABASE_FACT` -- nothing here
involves an LLM; a finding is a typed, human-readable rendering of a
statistic `agent.insight.summarize_result` already computed
deterministically. See `analytics/provider.py` for the adapter that
builds these.

**Prompt 13** (`13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md`) extended this
module additively -- every pre-existing field/enum value/construction
call keeps working unchanged (verified by re-running
`tests/test_analytics_models.py`/`test_analytics_provider.py`
unmodified): `ResultShape` (what kind of result these findings are about),
five new `AnalyticsFindingKind` values and their typed sub-stats
(`MedianStat`/`PercentileStat`/`DistinctCountStat`/`GrowthStat`/
`RankingStat`, each carrying its own `formula: str` -- "record formula ...
metadata" per that prompt's own requirement), a richer `OutlierFinding`
(distinct from the legacy single-method `outlier: OutlierStat` field --
see its own docstring for why two outlier shapes coexist), and
`AnalyticsResult.shape`/`engine_version`/`insufficient_data_reasons`.
`analytics/engine.py` is the actual computation that produces these new
shapes; this module stays pure data contracts, same split as before.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent.insight import OutlierStat, TrendStat
from agent.provenance import ProvenancedClaim

#: The typed-contract schema version for everything `analytics.engine`
#: produces -- stamped onto every `AnalyticsResult.engine_version` (Prompt
#: 13's own "record ... version metadata" requirement, master-contract
#: rule 11). Lives here, not in `analytics/engine.py`, so the one engine
#: module can import it from its own contracts module rather than the
#: reverse (avoiding a circular import) -- bump this when a computed
#: finding's formula/shape changes in a way a consumer should be able to
#: detect.
ANALYTICS_ENGINE_VERSION = "1.0.0"


class ResultShape(str, Enum):
    """What shape of query result a set of findings is about -- Prompt 13's
    own classification (`analytics.engine.classify_result_shape`), a
    closed set like `AnalyticsFindingKind` below.

    Attributes:
        SCALAR: A single row (any number of columns) -- one data point,
            nothing to rank/trend against.
        TIME_SERIES: Multiple rows, a single label/value pair whose label
            column is period-shaped (a year, a year-month, or an ISO
            date) for a majority of rows.
        CATEGORICAL_AGGREGATE: Multiple rows, a single label/value pair
            whose label column is not period-shaped (e.g. "revenue by
            product category").
        MULTIDIMENSIONAL: Multiple rows, a value column plus two or more
            non-numeric/dimension columns (e.g. "revenue by category and
            region") -- ranked via a flattened composite label, not true
            N-way pivot/cube analysis (a disclosed, bounded scope).
        RAW_TABLE: No usable label/value pair exists (e.g. every column
            is numeric, or every column is non-numeric) -- only
            per-column stats are computed, nothing row-level.
        EMPTY: Zero rows.
    """

    SCALAR = "scalar"
    TIME_SERIES = "time_series"
    CATEGORICAL_AGGREGATE = "categorical_aggregate"
    MULTIDIMENSIONAL = "multidimensional"
    RAW_TABLE = "raw_table"
    EMPTY = "empty"


class AnalyticsFindingKind(str, Enum):
    """What kind of statistical finding one `AnalyticsFinding` reports.

    A closed set, not a free-form string, mirroring
    `retrieval.models.ChunkType`'s own "deliberately closed" rationale --
    a caller (a future UI trend badge, an eval script) can exhaustively
    switch on this instead of string-matching.

    `MEDIAN`/`PERCENTILE`/`DISTINCT_COUNT`/`GROWTH`/`RANKING` (Prompt 13)
    are additive -- only ever produced by `analytics.engine
    .compute_analytics_result`, never by the pre-existing
    `ResultSummaryAnalyticsProvider`.
    """

    TREND = "trend"
    OUTLIER = "outlier"
    VARIANCE = "variance"
    MEDIAN = "median"
    PERCENTILE = "percentile"
    DISTINCT_COUNT = "distinct_count"
    GROWTH = "growth"
    RANKING = "ranking"
    COLUMN_SUMMARY = "column_summary"
    #: Prompt 14 (14_ANOMALY_ROOT_CAUSE_CONTRACT.md) -- only ever produced
    #: by `analytics.anomaly.detect_anomalies` (via `analytics.engine
    #: .compute_analytics_result`'s TIME_SERIES branch), one finding per
    #: flagged point -- see `AnomalyPoint`'s own docstring.
    ANOMALY = "anomaly"


class ColumnSummaryStat(BaseModel):
    """One column's basic shape -- row count, null count, and (for a
    numeric column) min/max/mean. The remaining per-column statistics
    (median, percentiles, distinct count, variance/stddev) each get their
    own dedicated finding/kind above/below rather than being folded in
    here, matching how `VARIANCE` already stood alone as its own kind
    before this prompt."""

    model_config = ConfigDict(frozen=True)

    column: str
    count: int
    null_count: int
    is_numeric: bool
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None


class VarianceStat(BaseModel):
    """One column's variance/stddev/coefficient-of-variation, typed --
    unlike the legacy `VARIANCE` kind's `variance_column: str` field
    (text-only, see `AnalyticsFinding`'s own docstring), this new-engine
    counterpart carries the real numbers, matching every other Prompt-13
    finding's own typed shape."""

    model_config = ConfigDict(frozen=True)

    column: str
    variance: float
    stddev: float
    coefficient_of_variation: float | None = None
    formula: str = "population variance/stddev (statistics.pvariance/pstdev); CoV = stddev / |mean|"


class MedianStat(BaseModel):
    """One column's median -- `agent.insight.ColumnStat` has no median
    field at all; this closes that gap without touching the pinned
    module (see this package's own module docstring)."""

    model_config = ConfigDict(frozen=True)

    column: str
    median: float
    formula: str = "statistics.median(non-null numeric values)"


class PercentileStat(BaseModel):
    """One column's percentile breakdown -- `statistics.quantiles(...,
    n=100, method="inclusive")`, stdlib, no new dependency. `p50` is the
    same value `MedianStat.median` would report for the same column
    (kept as two separate findings/kinds anyway, matching how `median`
    and `mean`/`mode` are conventionally reported as distinct named
    statistics, not because the numbers themselves differ)."""

    model_config = ConfigDict(frozen=True)

    column: str
    p25: float
    p50: float
    p75: float
    p90: float
    p95: float
    p99: float
    formula: str = 'statistics.quantiles(non-null numeric values, n=100, method="inclusive")'


class DistinctCountStat(BaseModel):
    """One column's distinct-value count -- including a *numeric* column,
    a real gap `agent.insight.ColumnStat.distinct_count` has today (only
    ever set for non-numeric columns there)."""

    model_config = ConfigDict(frozen=True)

    column: str
    distinct_count: int


class GrowthPoint(BaseModel):
    """One period's value in a `GrowthStat.points` series.

    `change_percent_from_previous` is `None` for the series' first point
    (nothing to compare against) and also `None` when the *previous*
    point's value was exactly `0` (a percentage change from zero is
    undefined, the identical zero-denominator guard
    `agent.insight._compute_trend` already established) -- never a
    division-by-zero, never a misleading "infinite growth" claim.
    """

    model_config = ConfigDict(frozen=True)

    period: str
    value: float
    change_percent_from_previous: float | None = None


class GrowthStat(BaseModel):
    """A full period-by-period growth series -- `TIME_SERIES` shapes only.

    Unlike `agent.insight.TrendStat` (first-vs-last only), this is every
    period in the series. `missing_periods` is populated only when the
    label column matched one of the three recognized period patterns
    (4-digit year, `YYYY-MM`, ISO date) -- see `analytics.engine`'s own
    docstring for the recognized shapes and why an unrecognized label
    column still gets `points` (row-order-based, mirroring `TrendStat`'s
    own documented convention) just without gap detection.
    """

    model_config = ConfigDict(frozen=True)

    label_column: str
    value_column: str
    points: tuple[GrowthPoint, ...]
    overall_change_percent: float | None = None
    direction: Literal["up", "down", "flat"] | None = None
    missing_periods: tuple[str, ...] = ()
    formula: str = "change_percent = 100 * (current - previous) / abs(previous)"


class RankingEntry(BaseModel):
    """One label's rank within a `RankingStat`."""

    model_config = ConfigDict(frozen=True)

    label: str
    value: float
    rank: int
    share_percent: float | None = None


class RankingStat(BaseModel):
    """A full, ranked breakdown of every distinct label's total --
    `CATEGORICAL_AGGREGATE`/`MULTIDIMENSIONAL` shapes only. `share_percent`
    on each entry is also this result's "distribution" -- a distribution
    is the same ranked breakdown viewed by its share-of-total percentages,
    not a second, separately-computed structure (avoids computing and
    carrying the same underlying totals twice).

    For a `MULTIDIMENSIONAL` result, `label` is a flattened composite of
    every dimension column's value for that row (e.g. `"Bikes / West"`) --
    a disclosed, bounded approach, not true N-way pivot/cube analysis.
    """

    model_config = ConfigDict(frozen=True)

    label_column: str
    value_column: str
    entries: tuple[RankingEntry, ...]
    #: True when the real distinct-label count exceeded
    #: `Settings.analytics_ranking_max_entries` and `entries` was capped
    #: to the top N by value -- so a caller can tell "this is everything"
    #: from "this is the top N of more."
    truncated: bool = False
    formula: str = "share_percent = 100 * value / sum(all values); ranked descending by value"


class OutlierFinding(BaseModel):
    """One label flagged as a statistical outlier by the new engine --
    deliberately a **separate** shape from `agent.insight.OutlierStat`
    (kept as the `outlier` field below, unchanged, for the pre-existing
    `ResultSummaryAnalyticsProvider`): that one only ever reports a
    single z-score-based finding; this one is produced by
    `analytics.engine.compute_analytics_result`, which checks **both**
    z-score and IQR methods and tags which one flagged each label (a
    label can be flagged by one, the other, or both -- each becomes its
    own `OutlierFinding`).
    """

    model_config = ConfigDict(frozen=True)

    label: str
    value: float
    column: str
    method: Literal["zscore", "iqr"]
    #: Set only for `method == "zscore"`.
    deviations_from_mean: float | None = None
    #: Set only for `method == "iqr"` -- how far outside the
    #: `[Q1 - 1.5*IQR, Q3 + 1.5*IQR]` fence this value fell.
    iqr_distance: float | None = None
    formula: str


class AnomalyMethod(str, Enum):
    """Which of `analytics.anomaly.detect_anomalies`'s five independent
    detection methods flagged a point -- a closed set, like every other
    enum in this module. See that module's own docstring for the exact
    algorithm/configuration-flag per method.
    """

    THRESHOLD = "threshold"
    PERCENT_CHANGE = "percent_change"
    ROLLING_ZSCORE = "rolling_zscore"
    IQR = "iqr"
    SEASONAL = "seasonal"


class AnomalySignal(BaseModel):
    """One method's own evidence that a point is anomalous -- a point can
    carry several of these at once (one per method that flagged it),
    never collapsed into a single undifferentiated "is this anomalous"
    boolean."""

    model_config = ConfigDict(frozen=True)

    method: AnomalyMethod
    baseline_value: float
    actual_value: float
    #: A z-score, a percent change, or an absolute delta, depending on
    #: `method` -- always the number that was actually compared against
    #: `threshold_used` to decide whether this signal fires.
    deviation: float
    threshold_used: float
    formula: str


class AnomalyPoint(BaseModel):
    """One chronological point `analytics.anomaly.detect_anomalies`
    flagged -- carries every `AnomalySignal` that fired for it (always
    non-empty: a point only ever becomes an `AnomalyPoint` because at
    least one method flagged it)."""

    model_config = ConfigDict(frozen=True)

    period: str
    value: float
    signals: tuple[AnomalySignal, ...]


class AnomalyDetectionResult(BaseModel):
    """`analytics.anomaly.detect_anomalies`'s own return shape -- the
    direct result of scanning one chronological series, before any of its
    `anomalies` are folded into `AnalyticsFinding`s by `analytics.engine
    .compute_analytics_result`'s `TIME_SERIES` branch (which is the
    *only* live caller of `detect_anomalies` in this codebase today; a
    future caller with its own series, outside that one integration
    point, can use this function/result directly).

    Attributes:
        anomalies: Every flagged point, in series order -- empty (never
            `None`) when nothing was flagged, the same "always iterable"
            convention every other plural field in this module already
            uses.
        methods_evaluated: Which of the five `AnomalyMethod` values
            actually ran at least once (had enough history/configuration
            to evaluate anything) -- distinct from which methods *fired*
            (see each `AnomalyPoint.signals`).
        insufficient_data_reasons: Every method that was skipped
            entirely and why (e.g. `"iqr: fewer than 4 points"`,
            `"seasonal: no prior occurrence at lag 12"`) -- inspectable,
            not a silent omission.
        engine_version: `ANALYTICS_ENGINE_VERSION` at computation time.
    """

    model_config = ConfigDict(frozen=True)

    anomalies: tuple[AnomalyPoint, ...] = ()
    methods_evaluated: tuple[AnomalyMethod, ...] = ()
    insufficient_data_reasons: tuple[str, ...] = ()
    engine_version: str = ANALYTICS_ENGINE_VERSION


class AnalyticsFinding(BaseModel):
    """One statistical finding about a query result, already computed and
    already true of the data -- never an LLM's own claim about it.

    Exactly one of the per-kind fields below is set, matching `kind`.
    Modeled as one class with optional per-kind fields (rather than one
    class per kind) so `AnalyticsResult.findings` can be a single
    homogeneous tuple a caller iterates without an `isinstance` check per
    element -- `kind` alone is enough to know which field to read.
    """

    model_config = ConfigDict(frozen=True)

    kind: AnalyticsFindingKind
    #: A short, deterministic, human-readable rendering of this finding
    #: (e.g. "Sales rose 12.4% from 2011 to 2014."). Always
    #: `DataTruthLevel.DATABASE_FACT` for this module's own default
    #: provider -- see this module's own docstring.
    claim: ProvenancedClaim
    trend: TrendStat | None = None
    #: Legacy single-method outlier shape -- only ever set by the
    #: pre-existing `ResultSummaryAnalyticsProvider`. See `OutlierFinding`
    #: above for the new engine's own, richer shape.
    outlier: OutlierStat | None = None
    #: Set only when `kind == VARIANCE` -- the column this finding is about.
    #: Not a full `ColumnStat` (that would duplicate `stddev`/
    #: `coefficient_of_variation`, which already live in `claim.value`'s
    #: rendering) -- just enough to let a caller group variance findings by
    #: column without re-parsing `claim.value`. Only ever set by the
    #: legacy `ResultSummaryAnalyticsProvider`; the new engine's own
    #: `kind == VARIANCE` findings set `variance` (typed) instead.
    variance_column: str | None = None
    # --- Prompt 13 additions (13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md) ---
    column_summary: ColumnSummaryStat | None = None
    variance: VarianceStat | None = None
    median: MedianStat | None = None
    percentile: PercentileStat | None = None
    distinct_count: DistinctCountStat | None = None
    growth: GrowthStat | None = None
    ranking: RankingStat | None = None
    #: The new engine's own outlier shape -- see `OutlierFinding`'s own
    #: docstring for why this is distinct from the legacy `outlier` field.
    outlier_detail: OutlierFinding | None = None
    #: Set only when `kind == ANOMALY` -- Prompt 14
    #: (14_ANOMALY_ROOT_CAUSE_CONTRACT.md).
    anomaly: AnomalyPoint | None = None


class AnalyticsResult(BaseModel):
    """The full set of analytics findings for one query result.

    Attributes:
        row_count: The result's row count, carried alongside `findings` so
            a caller can distinguish "genuinely nothing to find" (e.g. a
            single-row aggregate) from "findings were computed but empty."
        findings: Every finding this result produced -- empty (never a
            sentinel `None`) when there was nothing to report, so a caller
            can always iterate this without a `None` check, the same
            convention `agent.insight.ResultSummary.outliers` already uses.
        shape: Which `ResultShape` these findings are about -- `None` for
            the pre-existing `ResultSummaryAnalyticsProvider` (it never
            classified a shape), set for every result
            `analytics.engine.compute_analytics_result` produces.
        engine_version: `ANALYTICS_ENGINE_VERSION` at the time these
            findings were computed (Prompt 13's "record ... version
            metadata" requirement) -- defaults to the current version, so
            the pre-existing adapter's own construction call needs no
            change to keep validating.
        insufficient_data_reasons: Every calculation `analytics.engine
            .compute_analytics_result` explicitly skipped and why (e.g.
            `"growth: fewer than 2 periods"`, `"outliers: fewer than 3
            categories"`) -- inspectable, not a silent `None`/omission.
            Always empty for the pre-existing adapter (it never skips
            anything explainable this way).
    """

    model_config = ConfigDict(frozen=True)

    row_count: int
    findings: tuple[AnalyticsFinding, ...] = ()
    shape: ResultShape | None = None
    engine_version: str = ANALYTICS_ENGINE_VERSION
    insufficient_data_reasons: tuple[str, ...] = ()


class ContributorStat(BaseModel):
    """One dimension value's contribution to the change between a
    baseline and a current period -- `analytics.root_cause
    .investigate_root_cause`'s per-contributor shape. Only ever appears
    inside `RootCauseResult.contributors` after clearing that function's
    own validation threshold (`Settings.root_cause_min_contribution_
    percent`) -- see `RootCauseResult.has_sufficient_evidence`'s own
    docstring for why a contributor that doesn't clear it is never
    constructed as one of these at all.
    """

    model_config = ConfigDict(frozen=True)

    label: str
    baseline_value: float
    current_value: float
    contribution: float
    contribution_percent: float
    rank: int


class RootCauseResult(BaseModel):
    """`analytics.root_cause.investigate_root_cause`'s full result --
    Prompt 14 (`14_ANOMALY_ROOT_CAUSE_CONTRACT.md`). Deliberately **not**
    wrapped in `AnalyticsFinding`/`AnalyticsResult`: this isn't a finding
    about one query result, it's a standalone investigation across two
    datasets (a current period and a baseline period) with its own
    shape -- forcing it through the single-dataset finding taxonomy would
    be a worse fit than its own small, clearly-named model.

    Attributes:
        has_sufficient_evidence: The literal acceptance-criterion switch
            -- `True` only when at least one contributor cleared
            `Settings.root_cause_min_contribution_percent`. `False`
            means `contributors` is empty and `confidence` is `None`,
            **by construction** (this function has no code path that
            populates either without this being `True` first) -- "no
            root cause is stated without supporting evidence."
        magnitude: `current_total - baseline_total` -- always populated
            (even when evidence is insufficient), so a caller can still
            show "here's what changed" without a confident "here's why."
        magnitude_percent: The same change, as a percentage of
            `baseline_total` -- `None` when `baseline_total` is `0` (a
            percentage change from zero is undefined, the identical
            guard `agent.insight._compute_trend` already established).
        baseline_total: Sum of the baseline period's value column.
        current_total: Sum of the current period's value column.
        contributors: Ranked, validated contributors only -- empty when
            `has_sufficient_evidence` is `False`.
        confidence: `None` when `has_sufficient_evidence` is `False`;
            otherwise the fraction of the total change the validated
            contributors collectively explain, `0.0`-`1.0`.
        limitations: Disclosed caveats -- e.g. how many contributors were
            excluded for not clearing the validation bar, or why evidence
            was insufficient at all. Always populated when `contributors`
            excludes anything found, never silently dropped.
        formula: How `confidence`/`contribution_percent` are computed.
        engine_version: `ANALYTICS_ENGINE_VERSION` at computation time.
    """

    model_config = ConfigDict(frozen=True)

    has_sufficient_evidence: bool
    magnitude: float | None = None
    magnitude_percent: float | None = None
    baseline_total: float | None = None
    current_total: float | None = None
    contributors: tuple[ContributorStat, ...] = ()
    confidence: float | None = None
    limitations: tuple[str, ...] = ()
    formula: str = (
        "contribution_percent = 100 * (current - baseline) / magnitude; "
        "confidence = min(1.0, sum(|contribution_percent| of validated contributors) / 100)"
    )
    engine_version: str = ANALYTICS_ENGINE_VERSION
