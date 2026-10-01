"""The Deterministic Analytical Result Engine -- Prompt 13
(`13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md`): `compute_analytics_result`
turns a raw SQL result (`columns`, `rows`) into a full, typed
`analytics.models.AnalyticsResult`, covering every result shape this
application's questions can produce (a single KPI, a time series, a
categorical breakdown, a multidimensional breakdown, or a raw table) and
the statistics the prompt names: row/null/distinct counts, min/max/mean/
median, variance/stddev, percentiles, a full period-by-period growth
series, a complete ranking (which doubles as a distribution, see
`analytics.models.RankingStat`'s own docstring), and outliers (both
z-score and IQR).

Pure, deterministic, no LLM, no I/O -- the same posture
`agent/insight.py` already establishes for its own (narrower) statistics.
**Deliberately independent of `agent.insight.summarize_result`, not a
reuse of its code**: that function's exact output is pinned to this
project's live-LLM benchmark numbers (see CLAUDE.md's "AI Data Analyst
depth" section) and must stay byte-identical; this module computes a
genuinely richer, null-aware superset from the same raw `columns`/`rows`
input instead of touching it. Shared *conventions* (population stddev,
the z-score outlier threshold of `2.0`) are kept consistent with
`agent.insight` by matching constant, not by import.

**Not called by `agent.llm_client`'s insight-narration prompt in this
pass** -- `agent.nodes.compute_analytics_node` stores this module's
output on `AgentState["analytical_result"]`/`AskResponse
.analytical_result`, but the live `_build_insight_prompt`/
`_INSIGHT_SYSTEM_PROMPT` text is untouched, for the identical
benchmark-stability reason the paragraph above gives. See that node's own
docstring for the full disclosed deferral.
"""

from __future__ import annotations

import re
import statistics as _statistics
from decimal import Decimal
from typing import Literal

from agent.provenance import DataTruthLevel, ProvenancedClaim
from analytics.anomaly import detect_anomalies
from analytics.models import (
    AnalyticsFinding,
    AnalyticsFindingKind,
    AnalyticsResult,
    ColumnSummaryStat,
    DistinctCountStat,
    GrowthPoint,
    GrowthStat,
    MedianStat,
    OutlierFinding,
    PercentileStat,
    RankingEntry,
    RankingStat,
    ResultShape,
    VarianceStat,
)
from config.settings import Settings, get_settings

_SOURCE = "analytics.engine.compute_analytics_result"

# Mirrors agent.insight._NUMERIC_TYPES exactly (by value, not by import --
# see this module's own docstring for why) -- bool is deliberately
# excluded, same reason: Python's bool is an int subclass, and a
# True/False column is not a quantity to aggregate.
_NUMERIC_TYPES = (int, float, Decimal)

#: Matches `agent.insight.OUTLIER_STDDEV_THRESHOLD` by constant, not
#: import -- see this module's own docstring.
OUTLIER_ZSCORE_THRESHOLD = 2.0
_MIN_LABELS_FOR_ZSCORE_OUTLIERS = 3
_MIN_LABELS_FOR_IQR_OUTLIERS = 4
_IQR_MULTIPLIER = 1.5

_PERCENTILE_RANKS = (25, 50, 75, 90, 95, 99)

# Three recognized period label shapes -- see `classify_period_column`'s
# own docstring for the disclosed, bounded scope (no general date-string
# parsing).
_YEAR_RE = re.compile(r"^\d{4}$")
_YEAR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ISO_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

#: A label column is treated as period-shaped when at least this fraction
#: of its (non-null) values match the same recognized pattern -- a
#: majority, not unanimity, so one stray non-conforming value (a "Total"
#: summary row, a typo) doesn't disqualify an otherwise clearly
#: chronological column.
_PERIOD_MAJORITY_THRESHOLD = 0.5

#: Hard safety cap on how many consecutive missing-period steps
#: `_detect_missing_periods` will walk for one gap, in case the label
#: column isn't actually sorted/sequential (e.g. duplicate or out-of-order
#: periods) -- bounds the loop, never a correctness guarantee about the
#: data itself.
_MAX_MISSING_PERIOD_STEPS = 1000


def _is_numeric_scalar(value: object) -> bool:
    return isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool)


def _non_null(values: list) -> list:
    return [v for v in values if v is not None]


def _column_is_numeric(values: list) -> bool:
    """A column counts as numeric when every *non-null* value is numeric
    -- an all-null column is treated as non-numeric (nothing to
    aggregate). Deliberately different from `agent.insight`'s stricter
    "every row (including any null) must be numeric" rule -- see this
    module's own docstring for why that difference is intentional and
    confined to this new, independent engine.
    """
    non_null = _non_null(values)
    return bool(non_null) and all(_is_numeric_scalar(v) for v in non_null)


def _classify_columns(columns: list[str], rows: list[tuple]) -> tuple[list[int], list[int]]:
    """Returns `(numeric_indices, non_numeric_indices)` for `columns`."""
    numeric_indices: list[int] = []
    non_numeric_indices: list[int] = []
    for idx in range(len(columns)):
        values = [row[idx] for row in rows]
        if _column_is_numeric(values):
            numeric_indices.append(idx)
        else:
            non_numeric_indices.append(idx)
    return numeric_indices, non_numeric_indices


def _pick_value_and_label_indices(
    columns: list[str], numeric_indices: list[int], non_numeric_indices: list[int]
) -> tuple[int | None, list[int]]:
    """Picks the "value" column (the *last* numeric column -- a GROUP BY
    dimension conventionally precedes its aggregate in a SELECT list, the
    identical convention `agent.insight.summarize_result` already
    establishes) and every "label"/dimension column.

    Falls back to the first other column as a single-element label list
    when there's no non-numeric column at all (a numeric-vs-numeric
    result, e.g. "year, total") -- the same fallback `agent.insight`
    already uses, so a result like that is still classified/ranked rather
    than silently treated as a raw table.

    Returns `(None, [])` when there's no numeric column at all (nothing
    to treat as a value).
    """
    if not numeric_indices:
        return None, []
    value_idx = numeric_indices[-1]
    if non_numeric_indices:
        return value_idx, list(non_numeric_indices)
    fallback = next((i for i in range(len(columns)) if i != value_idx), None)
    return value_idx, ([fallback] if fallback is not None else [])


def _period_kind(value: str) -> str | None:
    if _ISO_DATE_RE.match(value):
        return "date"
    if _YEAR_MONTH_RE.match(value):
        return "year_month"
    if _YEAR_RE.match(value):
        return "year"
    return None


def classify_period_column(labels: list[str]) -> str | None:
    """Returns the dominant recognized period pattern among `labels`
    (`"year"` / `"year_month"` / `"date"`), or `None` if no single
    pattern covers at least `_PERIOD_MAJORITY_THRESHOLD` of them.

    A disclosed, bounded scope: only these three label shapes are
    recognized at all -- no general natural-language date parsing (e.g.
    `"Jan 2021"`, `"Q1 2021"`) is attempted, to avoid a fragile,
    heavyweight date-parsing dependency for a feature whose main job is
    classification, not calendar arithmetic.
    """
    if not labels:
        return None
    counts: dict[str, int] = {}
    for label in labels:
        kind = _period_kind(label)
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    if not counts:
        return None
    best_kind, best_count = max(counts.items(), key=lambda item: item[1])
    if best_count / len(labels) >= _PERIOD_MAJORITY_THRESHOLD:
        return best_kind
    return None


def classify_result_shape(columns: list[str], rows: list[tuple]) -> ResultShape:
    """Classifies a raw SQL result into one of `analytics.models
    .ResultShape`'s six values -- see that enum's own docstring for the
    exact rule per shape."""
    if not rows:
        return ResultShape.EMPTY
    if len(rows) == 1:
        return ResultShape.SCALAR

    numeric_indices, non_numeric_indices = _classify_columns(columns, rows)
    value_idx, label_indices = _pick_value_and_label_indices(
        columns, numeric_indices, non_numeric_indices
    )
    if value_idx is None or not label_indices:
        return ResultShape.RAW_TABLE
    if len(label_indices) >= 2:
        return ResultShape.MULTIDIMENSIONAL

    label_idx = label_indices[0]
    label_strings = [str(row[label_idx]) for row in rows if row[label_idx] is not None]
    if classify_period_column(label_strings) is not None:
        return ResultShape.TIME_SERIES
    return ResultShape.CATEGORICAL_AGGREGATE


def _expected_next_period(period_kind: str, value: str) -> str | None:
    """The next expected label after `value`, for `period_kind in
    ("year", "year_month")` only -- `"date"` is a recognized
    classification shape (see `classify_period_column`) but deliberately
    gets no gap detection: a full ISO date's real reporting cadence
    (daily? weekly? month-end snapshots?) isn't inferrable from the label
    shape alone, and guessing wrong would produce a confidently incorrect
    "missing period" claim, worse than reporting none.
    """
    if period_kind == "year":
        return str(int(value) + 1)
    if period_kind == "year_month":
        year, month = (int(part) for part in value.split("-"))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        return f"{year:04d}-{month:02d}"
    return None


def _detect_missing_periods(period_kind: str, labels: list[str]) -> tuple[str, ...]:
    """Walks consecutive `labels` and reports every expected-but-absent
    step in between, bounded by `_MAX_MISSING_PERIOD_STEPS` per gap (a
    safety cap against a non-sequential/duplicate label column, not a
    correctness guarantee -- see that constant's own docstring)."""
    if period_kind not in ("year", "year_month"):
        return ()
    missing: list[str] = []
    for current, actual_next in zip(labels, labels[1:], strict=False):
        expected = _expected_next_period(period_kind, current)
        steps = 0
        while (
            expected is not None and expected != actual_next and steps < _MAX_MISSING_PERIOD_STEPS
        ):
            missing.append(expected)
            expected = _expected_next_period(period_kind, expected)
            steps += 1
    return tuple(missing)


def group_and_sum_by_label(
    columns: list[str], rows: list[tuple], value_idx: int, label_indices: list[int]
) -> tuple[dict[str, float], str, str]:
    """Groups+sums `rows` by their (possibly composite) label, in
    insertion/row order -- the same "re-aggregate already-aggregated SQL
    rows by label text" approach `agent.insight.summarize_result`
    already establishes, generalized to one or more label columns.

    A row whose label value is `None` is skipped entirely (nothing
    meaningful to rank/trend a null category by); a row whose value cell
    is `None` is also skipped (excluded from the sum, consistent with
    this module's own null-handling elsewhere).

    Returns `(totals_by_label, value_column_name, label_column_name)` --
    `label_column_name` is a `" / "`-joined composite of every label
    column's name for a multidimensional result.

    Public (Prompt 14, `14_ANOMALY_ROOT_CAUSE_CONTRACT.md`) -- reused
    verbatim by `analytics.root_cause.investigate_root_cause` for its own
    current-period/baseline-period dimensional breakdowns, rather than a
    second copy of the same grouping logic (master-contract rule 2/3).
    """
    label_column = " / ".join(columns[i] for i in label_indices)
    value_column = columns[value_idx]
    totals: dict[str, float] = {}
    for row in rows:
        if row[value_idx] is None or any(row[i] is None for i in label_indices):
            continue
        label = " / ".join(str(row[i]) for i in label_indices)
        totals[label] = totals.get(label, 0.0) + float(row[value_idx])
    return totals, value_column, label_column


def _column_summary_finding(
    name: str, count: int, null_count: int, is_numeric: bool, minimum, maximum, mean
) -> AnalyticsFinding:
    if is_numeric:
        text = (
            f"{name}: {count} value(s) ({null_count} null), min={minimum:g}, "
            f"max={maximum:g}, mean={mean:g}."
        )
    else:
        text = f"{name}: {count} value(s) ({null_count} null)."
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.COLUMN_SUMMARY,
        claim=ProvenancedClaim(value=text, level=DataTruthLevel.DATABASE_FACT, source=_SOURCE),
        column_summary=ColumnSummaryStat(
            column=name,
            count=count,
            null_count=null_count,
            is_numeric=is_numeric,
            minimum=minimum,
            maximum=maximum,
            mean=mean,
        ),
    )


def _distinct_count_finding(name: str, non_null_values: list) -> AnalyticsFinding:
    distinct = len(set(non_null_values))
    text = f"{name} has {distinct} distinct value(s)."
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.DISTINCT_COUNT,
        claim=ProvenancedClaim(value=text, level=DataTruthLevel.DATABASE_FACT, source=_SOURCE),
        distinct_count=DistinctCountStat(column=name, distinct_count=distinct),
    )


def _median_finding(name: str, nums: list[float]) -> AnalyticsFinding:
    median = round(_statistics.median(nums), 4)
    text = f"{name} has a median of {median:g}."
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.MEDIAN,
        claim=ProvenancedClaim(value=text, level=DataTruthLevel.DATABASE_FACT, source=_SOURCE),
        median=MedianStat(column=name, median=median),
    )


def _percentile_finding(name: str, nums: list[float]) -> AnalyticsFinding:
    # statistics.quantiles(data, n=100, method="inclusive") returns 99 cut
    # points; index k-1 is the k-th percentile.
    cut_points = _statistics.quantiles(nums, n=100, method="inclusive")
    p25, p50, p75, p90, p95, p99 = (round(cut_points[rank - 1], 4) for rank in _PERCENTILE_RANKS)
    text = f"{name}: p25={p25:g}, p50={p50:g}, p75={p75:g}, p90={p90:g}, p95={p95:g}, p99={p99:g}."
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.PERCENTILE,
        claim=ProvenancedClaim(value=text, level=DataTruthLevel.DATABASE_FACT, source=_SOURCE),
        percentile=PercentileStat(
            column=name, p25=p25, p50=p50, p75=p75, p90=p90, p95=p95, p99=p99
        ),
    )


def _variance_finding(name: str, nums: list[float]) -> AnalyticsFinding:
    variance = round(_statistics.pvariance(nums), 4)
    stddev = round(_statistics.pstdev(nums), 4)
    mean = sum(nums) / len(nums)
    coefficient_of_variation = round(stddev / abs(mean), 4) if mean != 0 else None
    text = f"{name} has a variance of {variance:g} (stddev {stddev:g})"
    if coefficient_of_variation is not None:
        text += f", coefficient of variation {coefficient_of_variation:g}"
    text += "."
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.VARIANCE,
        claim=ProvenancedClaim(value=text, level=DataTruthLevel.DATABASE_FACT, source=_SOURCE),
        variance=VarianceStat(
            column=name,
            variance=variance,
            stddev=stddev,
            coefficient_of_variation=coefficient_of_variation,
        ),
    )


def _column_findings(name: str, values: list, insufficient: list[str]) -> list[AnalyticsFinding]:
    """Every per-column finding for one column -- always a `COLUMN_SUMMARY`
    and a `DISTINCT_COUNT` (every column, every shape), plus `MEDIAN`/
    `PERCENTILE`/`VARIANCE` for a numeric column with >= 2 non-null
    values (fewer than that is recorded in `insufficient`, not silently
    skipped)."""
    findings: list[AnalyticsFinding] = []
    non_null = _non_null(values)
    null_count = len(values) - len(non_null)
    is_numeric = _column_is_numeric(values)
    nums = [float(v) for v in non_null] if is_numeric else []
    minimum = min(nums) if nums else None
    maximum = max(nums) if nums else None
    mean = (sum(nums) / len(nums)) if nums else None

    findings.append(
        _column_summary_finding(name, len(values), null_count, is_numeric, minimum, maximum, mean)
    )
    findings.append(_distinct_count_finding(name, non_null))

    if is_numeric:
        if len(nums) >= 2:
            findings.append(_median_finding(name, nums))
            findings.append(_percentile_finding(name, nums))
            findings.append(_variance_finding(name, nums))
        else:
            insufficient.append(
                f"{name}: median/percentile/variance require at least 2 non-null numeric "
                f"values (got {len(nums)})"
            )
    return findings


def _compute_growth_stat(
    totals_by_label: dict[str, float], label_column: str, value_column: str, insufficient: list[str]
) -> GrowthStat | None:
    """The full period-by-period growth series -- `TIME_SERIES` shapes
    only. Unlike `agent.insight._compute_trend` (first-vs-last only),
    every period gets its own `GrowthPoint`; `None` is returned (and the
    skip recorded) only when there are fewer than two periods at all.

    Returns the typed `GrowthStat` directly (not wrapped in an
    `AnalyticsFinding`) -- `_growth_finding` below wraps it for
    `compute_analytics_result`'s own findings list, while `compute_
    analytics_result` also reads `.points` directly off this return value
    to feed `analytics.anomaly.detect_anomalies` (Prompt 14,
    `14_ANOMALY_ROOT_CAUSE_CONTRACT.md`) -- splitting this out avoids a
    second, duplicate re-computation of the same series for that purpose.
    """
    if len(totals_by_label) < 2:
        insufficient.append("growth: fewer than 2 periods")
        return None

    labels = list(totals_by_label)
    values = [totals_by_label[label] for label in labels]
    points: list[GrowthPoint] = [GrowthPoint(period=labels[0], value=values[0])]
    for period, value, previous_value in zip(labels[1:], values[1:], values[:-1], strict=True):
        change_percent = (
            round(100 * (value - previous_value) / abs(previous_value), 1)
            if previous_value != 0
            else None
        )
        points.append(
            GrowthPoint(period=period, value=value, change_percent_from_previous=change_percent)
        )

    overall_change_percent = None
    direction: Literal["up", "down", "flat"] | None = None
    if values[0] != 0:
        overall_change_percent = round(100 * (values[-1] - values[0]) / abs(values[0]), 1)
        direction = (
            "up" if overall_change_percent > 0 else "down" if overall_change_percent < 0 else "flat"
        )

    period_kind = classify_period_column(labels)
    missing_periods = _detect_missing_periods(period_kind, labels) if period_kind else ()

    return GrowthStat(
        label_column=label_column,
        value_column=value_column,
        points=tuple(points),
        overall_change_percent=overall_change_percent,
        direction=direction,
        missing_periods=missing_periods,
    )


def _growth_finding(stat: GrowthStat) -> AnalyticsFinding:
    """Wraps an already-computed `GrowthStat` (see `_compute_growth_stat`
    above) into the `AnalyticsFinding` `compute_analytics_result` appends
    to its own findings list."""
    direction_word = {"up": "rose", "down": "fell", "flat": "stayed flat", None: "changed"}[
        stat.direction
    ]
    if stat.overall_change_percent is not None:
        text = (
            f"{stat.value_column} {direction_word} {abs(stat.overall_change_percent):.1f}% from "
            f"{stat.points[0].period} to {stat.points[-1].period}."
        )
    else:
        text = f"{stat.value_column} across {len(stat.points)} period(s) of {stat.label_column}."
    if stat.missing_periods:
        text += f" {len(stat.missing_periods)} period(s) appear to be missing from the series."

    return AnalyticsFinding(
        kind=AnalyticsFindingKind.GROWTH,
        claim=ProvenancedClaim(value=text, level=DataTruthLevel.DATABASE_FACT, source=_SOURCE),
        growth=stat,
    )


def _anomaly_findings(
    points: tuple[GrowthPoint, ...], column: str, settings: Settings
) -> list[AnalyticsFinding]:
    """Runs `analytics.anomaly.detect_anomalies` over an already-computed
    `GrowthStat.points` series (Prompt 14,
    `14_ANOMALY_ROOT_CAUSE_CONTRACT.md`) -- zero new data, zero new
    query. One `AnalyticsFinding` per flagged point, mirroring the
    one-finding-per-outlier precedent `_outlier_findings` already
    establishes (not one finding for the whole series)."""
    series = [(point.period, point.value) for point in points]
    result = detect_anomalies(series, settings)
    findings = []
    for anomaly in result.anomalies:
        methods = ", ".join(sorted({signal.method.value for signal in anomaly.signals}))
        text = (
            f"{column} at {anomaly.period} ({anomaly.value:g}) flagged as anomalous by: "
            f"{methods}."
        )
        findings.append(
            AnalyticsFinding(
                kind=AnalyticsFindingKind.ANOMALY,
                claim=ProvenancedClaim(
                    value=text, level=DataTruthLevel.DATABASE_FACT, source=_SOURCE
                ),
                anomaly=anomaly,
            )
        )
    return findings


def _ranking_finding(
    totals_by_label: dict[str, float], label_column: str, value_column: str, max_entries: int
) -> AnalyticsFinding:
    """The full ranked breakdown -- `CATEGORICAL_AGGREGATE`/
    `MULTIDIMENSIONAL` shapes only. Always produces a finding (unlike
    growth/outliers, which can be skipped): even a single category has a
    trivial 1-entry, 100%-share ranking worth reporting.
    """
    grand_total = sum(totals_by_label.values())
    ordered = sorted(totals_by_label.items(), key=lambda item: item[1], reverse=True)
    truncated = len(ordered) > max_entries
    entries = tuple(
        RankingEntry(
            label=label,
            value=value,
            rank=rank,
            share_percent=round(100 * value / grand_total, 1) if grand_total else None,
        )
        for rank, (label, value) in enumerate(ordered[:max_entries], start=1)
    )
    top = entries[0] if entries else None
    text = (
        f"{top.label} ranks first on {value_column} at {top.value:g}"
        + (f" ({top.share_percent:g}% of the total)" if top.share_percent is not None else "")
        + "."
        if top is not None
        else f"No ranking available for {value_column}."
    )
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.RANKING,
        claim=ProvenancedClaim(value=text, level=DataTruthLevel.DATABASE_FACT, source=_SOURCE),
        ranking=RankingStat(
            label_column=label_column,
            value_column=value_column,
            entries=entries,
            truncated=truncated,
        ),
    )


def _outlier_findings(
    totals_by_label: dict[str, float], column: str, insufficient: list[str]
) -> list[AnalyticsFinding]:
    """Both z-score and IQR outlier detection over the same per-label
    totals the ranking is built from -- `CATEGORICAL_AGGREGATE`/
    `MULTIDIMENSIONAL` shapes only. A label can be flagged by one method,
    the other, both, or neither; each flag becomes its own finding."""
    findings: list[AnalyticsFinding] = []
    values = list(totals_by_label.values())

    if len(totals_by_label) >= _MIN_LABELS_FOR_ZSCORE_OUTLIERS:
        mean = sum(values) / len(values)
        stddev = _statistics.pstdev(values)
        if stddev == 0:
            insufficient.append("outliers (zscore): all values identical (stddev is 0)")
        else:
            for label, value in totals_by_label.items():
                deviations = (value - mean) / stddev
                if abs(deviations) > OUTLIER_ZSCORE_THRESHOLD:
                    findings.append(
                        _outlier_finding(
                            label, value, column, "zscore", deviations=round(deviations, 2)
                        )
                    )
    else:
        insufficient.append(
            f"outliers (zscore): fewer than {_MIN_LABELS_FOR_ZSCORE_OUTLIERS} categories"
        )

    if len(totals_by_label) >= _MIN_LABELS_FOR_IQR_OUTLIERS:
        q1, _, q3 = _statistics.quantiles(values, n=4, method="inclusive")
        iqr = q3 - q1
        if iqr == 0:
            insufficient.append("outliers (iqr): interquartile range is 0")
        else:
            lower_fence = q1 - _IQR_MULTIPLIER * iqr
            upper_fence = q3 + _IQR_MULTIPLIER * iqr
            for label, value in totals_by_label.items():
                if value < lower_fence or value > upper_fence:
                    fence_distance = round(
                        min(abs(value - lower_fence), abs(value - upper_fence)), 2
                    )
                    findings.append(
                        _outlier_finding(label, value, column, "iqr", iqr_distance=fence_distance)
                    )
    else:
        insufficient.append(f"outliers (iqr): fewer than {_MIN_LABELS_FOR_IQR_OUTLIERS} categories")

    return findings


def _outlier_finding(
    label: str,
    value: float,
    column: str,
    method: str,
    *,
    deviations: float | None = None,
    iqr_distance: float | None = None,
) -> AnalyticsFinding:
    if method == "zscore":
        text = f"{label} is an outlier on {column} ({method}): {value:g} ({deviations:+.2f} std devs from the mean)."
        formula = "z = (value - mean) / population_stddev; flagged when |z| > 2.0"
    else:
        text = f"{label} is an outlier on {column} ({method}): {value:g} ({iqr_distance:g} beyond the IQR fence)."
        formula = "flagged when value is outside [Q1 - 1.5*IQR, Q3 + 1.5*IQR]"
    return AnalyticsFinding(
        kind=AnalyticsFindingKind.OUTLIER,
        claim=ProvenancedClaim(value=text, level=DataTruthLevel.DATABASE_FACT, source=_SOURCE),
        outlier_detail=OutlierFinding(
            label=label,
            value=value,
            column=column,
            method=method,  # type: ignore[arg-type]
            deviations_from_mean=deviations,
            iqr_distance=iqr_distance,
            formula=formula,
        ),
    )


def compute_analytics_result(
    columns: list[str], rows: list[tuple], settings: Settings | None = None
) -> AnalyticsResult:
    """The engine's single entry point: classifies `columns`/`rows` into a
    `ResultShape` and computes every applicable finding for it.

    Args:
        columns: Column names, in result order.
        rows: Result rows. Only ever read here -- never retained or
            forwarded whole anywhere (same "aggregate-only" posture
            `agent.insight.summarize_result` already establishes).
        settings: Defaults to `config.settings.get_settings()` -- accepted
            explicitly so a caller (or a test) can override
            `analytics_ranking_max_entries` without touching process-wide
            settings.

    Returns:
        A fully populated `AnalyticsResult` -- `shape` and `engine_version`
        always set, `insufficient_data_reasons` listing every calculation
        this call explicitly skipped and why (never a silent omission).
    """
    settings = settings or get_settings()
    shape = classify_result_shape(columns, rows)
    insufficient: list[str] = []

    if shape == ResultShape.EMPTY:
        insufficient.append("all calculations: empty result")
        return AnalyticsResult(
            row_count=0, findings=(), shape=shape, insufficient_data_reasons=tuple(insufficient)
        )

    findings: list[AnalyticsFinding] = []
    for idx, name in enumerate(columns):
        values = [row[idx] for row in rows]
        findings.extend(_column_findings(name, values, insufficient))

    if shape in (
        ResultShape.TIME_SERIES,
        ResultShape.CATEGORICAL_AGGREGATE,
        ResultShape.MULTIDIMENSIONAL,
    ):
        numeric_indices, non_numeric_indices = _classify_columns(columns, rows)
        value_idx, label_indices = _pick_value_and_label_indices(
            columns, numeric_indices, non_numeric_indices
        )
        # value_idx/label_indices are guaranteed non-degenerate here --
        # classify_result_shape already required a usable pair to reach
        # any of these three shapes.
        totals_by_label, value_column, label_column = group_and_sum_by_label(
            columns, rows, value_idx, label_indices  # type: ignore[arg-type]
        )

        if shape == ResultShape.TIME_SERIES:
            growth_stat = _compute_growth_stat(
                totals_by_label, label_column, value_column, insufficient
            )
            if growth_stat is not None:
                findings.append(_growth_finding(growth_stat))
                if settings.enable_anomaly_detection:
                    findings.extend(_anomaly_findings(growth_stat.points, value_column, settings))
        else:
            findings.append(
                _ranking_finding(
                    totals_by_label,
                    label_column,
                    value_column,
                    settings.analytics_ranking_max_entries,
                )
            )
            findings.extend(_outlier_findings(totals_by_label, value_column, insufficient))

    return AnalyticsResult(
        row_count=len(rows),
        findings=tuple(findings),
        shape=shape,
        insufficient_data_reasons=tuple(insufficient),
    )
