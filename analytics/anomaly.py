"""A general-purpose, configurable anomaly detector over a chronological
series -- Prompt 14 (`14_ANOMALY_ROOT_CAUSE_CONTRACT.md`).

Distinct from `analytics.engine`'s own categorical z-score/IQR outlier
detection (Prompt 13), which looks at one *static* set of per-label
totals with no notion of time order. This module scans a **chronological**
series point by point, checking each against up to five independent,
individually-configurable methods -- a flat threshold, point-over-point
percent change, a rolling (not global) z-score baseline, a whole-series
IQR fence, and an opt-in seasonality-aware comparison. Pure, deterministic,
no LLM, no I/O -- the same posture every other module under `analytics/`
already establishes.

**The one live caller today** is `analytics.engine.compute_analytics_result`'s
`TIME_SERIES` branch, which feeds this module the exact same
`GrowthStat.points` series it already computed -- zero new queries, zero
new data. A future caller with its own series can call
`detect_anomalies` directly.
"""

from __future__ import annotations

import statistics as _statistics

from analytics.models import (
    ANALYTICS_ENGINE_VERSION,
    AnomalyDetectionResult,
    AnomalyMethod,
    AnomalyPoint,
    AnomalySignal,
)
from config.settings import Settings, get_settings

#: Minimum points needed for *any* whole-series IQR computation --
#: matches `analytics.engine._MIN_LABELS_FOR_IQR_OUTLIERS`'s identical
#: convention for the categorical outlier check (Prompt 13), kept
#: consistent rather than re-derived.
_MIN_POINTS_FOR_IQR = 4


def _threshold_signals(
    values: list[float],
    settings: Settings,
    methods_evaluated: set[AnomalyMethod],
    insufficient: list[str],
) -> dict[int, AnomalySignal]:
    signals: dict[int, AnomalySignal] = {}
    threshold = settings.anomaly_absolute_threshold
    if threshold is None:
        insufficient.append("threshold: anomaly_absolute_threshold not configured")
        return signals
    if len(values) < 2:
        insufficient.append("threshold: fewer than 2 points")
        return signals
    methods_evaluated.add(AnomalyMethod.THRESHOLD)
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        if abs(delta) > threshold:
            signals[i] = AnomalySignal(
                method=AnomalyMethod.THRESHOLD,
                baseline_value=values[i - 1],
                actual_value=values[i],
                deviation=round(delta, 4),
                threshold_used=threshold,
                formula="flagged when |value - previous_value| > threshold",
            )
    return signals


def _percent_change_signals(
    values: list[float],
    settings: Settings,
    methods_evaluated: set[AnomalyMethod],
    insufficient: list[str],
) -> dict[int, AnomalySignal]:
    signals: dict[int, AnomalySignal] = {}
    if len(values) < 2:
        insufficient.append("percent_change: fewer than 2 points")
        return signals
    methods_evaluated.add(AnomalyMethod.PERCENT_CHANGE)
    threshold = settings.anomaly_percent_change_threshold
    for i in range(1, len(values)):
        previous = values[i - 1]
        if previous == 0:
            continue
        percent = round(100 * (values[i] - previous) / abs(previous), 1)
        if abs(percent) > threshold:
            signals[i] = AnomalySignal(
                method=AnomalyMethod.PERCENT_CHANGE,
                baseline_value=previous,
                actual_value=values[i],
                deviation=percent,
                threshold_used=threshold,
                formula="flagged when |100 * (value - previous) / |previous|| > threshold",
            )
    return signals


def _rolling_zscore_signals(
    values: list[float],
    settings: Settings,
    methods_evaluated: set[AnomalyMethod],
    insufficient: list[str],
) -> dict[int, AnomalySignal]:
    signals: dict[int, AnomalySignal] = {}
    window = settings.anomaly_rolling_window
    if len(values) <= window:
        insufficient.append(f"rolling_zscore: fewer than {window + 1} points")
        return signals
    methods_evaluated.add(AnomalyMethod.ROLLING_ZSCORE)
    threshold = settings.anomaly_zscore_threshold
    for i in range(window, len(values)):
        prior = values[i - window : i]
        mean = sum(prior) / window
        stddev = _statistics.pstdev(prior)
        if stddev == 0:
            continue
        z = (values[i] - mean) / stddev
        if abs(z) > threshold:
            signals[i] = AnomalySignal(
                method=AnomalyMethod.ROLLING_ZSCORE,
                baseline_value=round(mean, 4),
                actual_value=values[i],
                deviation=round(z, 2),
                threshold_used=threshold,
                formula=(
                    f"z = (value - mean of the prior {window} point(s)) / their population "
                    "stddev; flagged when |z| > threshold"
                ),
            )
    return signals


def _iqr_signals(
    values: list[float],
    settings: Settings,
    methods_evaluated: set[AnomalyMethod],
    insufficient: list[str],
) -> dict[int, AnomalySignal]:
    signals: dict[int, AnomalySignal] = {}
    if len(values) < _MIN_POINTS_FOR_IQR:
        insufficient.append(f"iqr: fewer than {_MIN_POINTS_FOR_IQR} points")
        return signals
    q1, _median, q3 = _statistics.quantiles(values, n=4, method="inclusive")
    iqr = q3 - q1
    if iqr == 0:
        insufficient.append("iqr: interquartile range is 0")
        return signals
    methods_evaluated.add(AnomalyMethod.IQR)
    multiplier = settings.anomaly_iqr_multiplier
    lower_fence = q1 - multiplier * iqr
    upper_fence = q3 + multiplier * iqr
    midpoint = round((q1 + q3) / 2, 4)
    for i, value in enumerate(values):
        if value < lower_fence or value > upper_fence:
            distance = round(min(abs(value - lower_fence), abs(value - upper_fence)), 4)
            signals[i] = AnomalySignal(
                method=AnomalyMethod.IQR,
                baseline_value=midpoint,
                actual_value=value,
                deviation=distance,
                threshold_used=multiplier,
                formula="flagged when value is outside [Q1 - k*IQR, Q3 + k*IQR] (whole series)",
            )
    return signals


def _seasonal_signals(
    values: list[float],
    settings: Settings,
    methods_evaluated: set[AnomalyMethod],
    insufficient: list[str],
) -> dict[int, AnomalySignal]:
    signals: dict[int, AnomalySignal] = {}
    lag = settings.anomaly_seasonal_period
    if not lag:
        return signals  # off by default -- not even an insufficient-data note, it's simply unconfigured
    if len(values) <= lag:
        insufficient.append(f"seasonal: no prior occurrence at lag {lag}")
        return signals
    methods_evaluated.add(AnomalyMethod.SEASONAL)
    zscore_threshold = settings.anomaly_zscore_threshold
    percent_threshold = settings.anomaly_percent_change_threshold
    for i in range(lag, len(values)):
        occurrences = []
        k = 1
        while i - k * lag >= 0:
            occurrences.append(values[i - k * lag])
            k += 1
        if len(occurrences) >= 2:
            mean = sum(occurrences) / len(occurrences)
            stddev = _statistics.pstdev(occurrences)
            if stddev == 0:
                continue
            z = (values[i] - mean) / stddev
            if abs(z) > zscore_threshold:
                signals[i] = AnomalySignal(
                    method=AnomalyMethod.SEASONAL,
                    baseline_value=round(mean, 4),
                    actual_value=values[i],
                    deviation=round(z, 2),
                    threshold_used=zscore_threshold,
                    formula=(
                        f"z = (value - mean of {len(occurrences)} same-lag-{lag} prior "
                        "occurrence(s)) / their population stddev; flagged when |z| > threshold"
                    ),
                )
        else:
            previous = occurrences[0]
            if previous == 0:
                continue
            percent = round(100 * (values[i] - previous) / abs(previous), 1)
            if abs(percent) > percent_threshold:
                signals[i] = AnomalySignal(
                    method=AnomalyMethod.SEASONAL,
                    baseline_value=previous,
                    actual_value=values[i],
                    deviation=percent,
                    threshold_used=percent_threshold,
                    formula=(
                        f"only one prior occurrence at lag {lag} -- percent change vs that single "
                        "point (lower confidence than the 2+-occurrence seasonal z-score above); "
                        "flagged when |percent| > threshold"
                    ),
                )
    return signals


def detect_anomalies(
    points: list[tuple[str, float]], settings: Settings | None = None
) -> AnomalyDetectionResult:
    """Scans `points` (a chronological `(period, value)` series, oldest
    first) against every configured method and returns every flagged
    point.

    Args:
        points: The series to scan, in chronological order -- the same
            ordering `analytics.engine.GrowthStat.points` already
            guarantees for a `TIME_SERIES` result.
        settings: Defaults to `config.settings.get_settings()`.

    Returns:
        An `AnomalyDetectionResult` listing every flagged point (never a
        bare boolean -- each carries its own `AnomalySignal`(s), so a
        caller always sees *why*), which methods actually ran, and why
        any method was skipped entirely.
    """
    settings = settings or get_settings()
    periods = [p[0] for p in points]
    values = [p[1] for p in points]

    methods_evaluated: set[AnomalyMethod] = set()
    insufficient: list[str] = []
    signals_by_index: dict[int, list[AnomalySignal]] = {}

    for signal_map in (
        _threshold_signals(values, settings, methods_evaluated, insufficient),
        _percent_change_signals(values, settings, methods_evaluated, insufficient),
        _rolling_zscore_signals(values, settings, methods_evaluated, insufficient),
        _iqr_signals(values, settings, methods_evaluated, insufficient),
        _seasonal_signals(values, settings, methods_evaluated, insufficient),
    ):
        for index, signal in signal_map.items():
            signals_by_index.setdefault(index, []).append(signal)

    anomalies = tuple(
        AnomalyPoint(period=periods[i], value=values[i], signals=tuple(signals_by_index[i]))
        for i in sorted(signals_by_index)
    )
    return AnomalyDetectionResult(
        anomalies=anomalies,
        methods_evaluated=tuple(sorted(methods_evaluated, key=lambda m: m.value)),
        insufficient_data_reasons=tuple(insufficient),
        engine_version=ANALYTICS_ENGINE_VERSION,
    )
