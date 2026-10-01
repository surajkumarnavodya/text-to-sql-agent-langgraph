"""Unit tests for `analytics/anomaly.py` -- Prompt 14
(`14_ANOMALY_ROOT_CAUSE_CONTRACT.md`)'s general-purpose, configurable
anomaly detector.

One class per method (threshold/percent-change/rolling-zscore/IQR/
seasonal): a clear anomaly case, a clear no-anomaly case, and an
insufficient-data case -- matching the prompt's own explicit testing
requirement.
"""

from __future__ import annotations

from analytics.anomaly import detect_anomalies
from analytics.models import AnomalyMethod

from config.settings import Settings


def _points(values: list[float]) -> list[tuple[str, float]]:
    return [(str(i), v) for i, v in enumerate(values)]


def _methods_at(result, period: str) -> set[str]:
    point = next(a for a in result.anomalies if a.period == period)
    return {s.method.value for s in point.signals}


class TestThresholdMethod:
    def test_flags_a_delta_beyond_the_threshold(self):
        settings = Settings(anomaly_absolute_threshold=10.0)
        points = _points([10.0, 11.0, 50.0])
        result = detect_anomalies(points, settings)
        assert AnomalyMethod.THRESHOLD in result.methods_evaluated
        assert "threshold" in _methods_at(result, "2")

    def test_no_anomaly_for_a_stable_series(self):
        settings = Settings(anomaly_absolute_threshold=10.0)
        points = _points([10.0, 11.0, 10.5, 11.2])
        result = detect_anomalies(points, settings)
        assert result.anomalies == ()

    def test_insufficient_data_when_not_configured(self):
        settings = Settings(anomaly_absolute_threshold=None)
        result = detect_anomalies(_points([1.0, 2.0, 3.0]), settings)
        assert AnomalyMethod.THRESHOLD not in result.methods_evaluated
        assert any("threshold" in r for r in result.insufficient_data_reasons)

    def test_insufficient_data_with_fewer_than_two_points(self):
        settings = Settings(anomaly_absolute_threshold=10.0)
        result = detect_anomalies(_points([1.0]), settings)
        assert AnomalyMethod.THRESHOLD not in result.methods_evaluated
        assert any("threshold: fewer than 2 points" in r for r in result.insufficient_data_reasons)


class TestPercentChangeMethod:
    def test_flags_a_large_percent_jump(self):
        settings = Settings(anomaly_percent_change_threshold=20.0)
        points = _points([100.0, 105.0, 200.0])
        result = detect_anomalies(points, settings)
        assert "percent_change" in _methods_at(result, "2")

    def test_no_anomaly_for_small_changes(self):
        settings = Settings(anomaly_percent_change_threshold=20.0)
        points = _points([100.0, 105.0, 98.0, 103.0])
        result = detect_anomalies(points, settings)
        assert result.anomalies == ()

    def test_insufficient_data_with_fewer_than_two_points(self):
        result = detect_anomalies(_points([5.0]))
        assert any(
            "percent_change: fewer than 2 points" in r for r in result.insufficient_data_reasons
        )

    def test_zero_previous_value_never_crashes_or_flags(self):
        settings = Settings(anomaly_percent_change_threshold=20.0)
        points = _points([0.0, 50.0])
        result = detect_anomalies(points, settings)
        assert "percent_change" not in _methods_at(result, "1") if result.anomalies else True


class TestRollingZscoreMethod:
    def test_flags_a_point_far_from_its_rolling_baseline(self):
        settings = Settings(anomaly_rolling_window=3, anomaly_zscore_threshold=2.0)
        points = _points([10.0, 11.0, 9.0, 10.5, 10.2, 1000.0])
        result = detect_anomalies(points, settings)
        assert AnomalyMethod.ROLLING_ZSCORE in result.methods_evaluated
        assert "rolling_zscore" in _methods_at(result, "5")

    def test_no_rolling_zscore_anomaly_for_a_stable_series(self):
        settings = Settings(anomaly_rolling_window=3, anomaly_zscore_threshold=2.0)
        points = _points([10.0, 11.0, 9.0, 10.5, 10.2, 10.1])
        result = detect_anomalies(points, settings)
        assert "rolling_zscore" not in {s.method.value for a in result.anomalies for s in a.signals}

    def test_insufficient_data_with_fewer_than_window_plus_one_points(self):
        settings = Settings(anomaly_rolling_window=5)
        result = detect_anomalies(_points([1.0, 2.0, 3.0]), settings)
        assert AnomalyMethod.ROLLING_ZSCORE not in result.methods_evaluated
        assert any(
            "rolling_zscore: fewer than 6 points" in r for r in result.insufficient_data_reasons
        )

    def test_known_limitation_a_sustained_linear_trend_can_still_be_flagged(self):
        """Disclosed, not hidden: the rolling method compares each point
        to the flat mean/stddev of its prior window -- it has no
        detrending step. A perfectly linear, constant-slope trend
        produces a *constant* rolling z-score at every point once the
        window is full (here, ~2.45 for window=3, a mathematical
        property of evenly-spaced data, not a bug) -- this is weaker
        protection than a trend-aware method would give, a real,
        disclosed limitation (see 14_ANOMALY_ROOT_CAUSE_CONTRACT.md),
        not a false claim that rolling baselines are trend-immune. It is
        still strictly better than a *global* mean, which would flag an
        even wider swath of a long trending series (see the next test)."""
        settings = Settings(anomaly_rolling_window=3, anomaly_zscore_threshold=2.0)
        points = _points([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0])
        result = detect_anomalies(points, settings)
        assert "rolling_zscore" in {s.method.value for a in result.anomalies for s in a.signals}

    def test_rolling_baseline_recovers_faster_than_a_global_one_would(self):
        """A one-off spike shifts the rolling window's own mean/stddev
        for only `window` subsequent points -- after that, later normal
        points are judged against a baseline that has already "forgotten"
        the spike, unlike a global mean, which would stay permanently
        skewed by it."""
        settings = Settings(anomaly_rolling_window=3, anomaly_zscore_threshold=2.0)
        points = _points([10.0, 11.0, 9.0, 500.0, 10.0, 11.0, 9.0, 10.0])
        result = detect_anomalies(points, settings)
        # The points well after the spike (indices 6, 7) must not still be
        # flagged once the spike has rolled out of their own window.
        assert "6" not in {
            a.period
            for a in result.anomalies
            if "rolling_zscore" in {s.method.value for s in a.signals}
        }
        assert "7" not in {
            a.period
            for a in result.anomalies
            if "rolling_zscore" in {s.method.value for s in a.signals}
        }


class TestIqrMethod:
    def test_flags_a_point_outside_the_fence(self):
        settings = Settings(anomaly_iqr_multiplier=1.5)
        points = _points([10.0, 11.0, 9.0, 10.5, 200.0])
        result = detect_anomalies(points, settings)
        assert AnomalyMethod.IQR in result.methods_evaluated
        assert "iqr" in _methods_at(result, "4")

    def test_no_anomaly_for_tight_values(self):
        settings = Settings(anomaly_iqr_multiplier=1.5)
        points = _points([10.0, 10.1, 9.9, 10.05, 9.95])
        result = detect_anomalies(points, settings)
        assert "iqr" not in {s.method.value for a in result.anomalies for s in a.signals}

    def test_insufficient_data_with_fewer_than_four_points(self):
        result = detect_anomalies(_points([1.0, 2.0, 3.0]))
        assert any("iqr: fewer than 4 points" in r for r in result.insufficient_data_reasons)

    def test_identical_values_record_zero_iqr_insufficiency(self):
        result = detect_anomalies(_points([5.0, 5.0, 5.0, 5.0]))
        assert any("interquartile range is 0" in r for r in result.insufficient_data_reasons)


class TestSeasonalMethod:
    def test_off_by_default_no_insufficiency_note(self):
        result = detect_anomalies(_points([1.0, 2.0, 3.0]))
        assert AnomalyMethod.SEASONAL not in result.methods_evaluated
        assert not any("seasonal" in r for r in result.insufficient_data_reasons)

    def test_flags_a_point_far_from_its_seasonal_mean(self):
        settings = Settings(anomaly_seasonal_period=4, anomaly_zscore_threshold=2.0)
        # 3 full cycles of a seasonal pattern with slight natural
        # variation (needed so the 3 same-lag prior occurrences have a
        # non-zero stddev -- an exactly-identical prior history makes the
        # seasonal z-score mathematically degenerate, correctly skipped
        # rather than flagged, same as every other zero-stddev guard in
        # this module), then a spike at the same seasonal index.
        values = [10.0, 20.0, 30.0, 39.0, 10.0, 20.0, 30.0, 41.0, 10.0, 20.0, 30.0, 40.0] + [
            10.0,
            20.0,
            30.0,
            999.0,
        ]
        result = detect_anomalies(_points(values), settings)
        assert AnomalyMethod.SEASONAL in result.methods_evaluated
        assert "seasonal" in _methods_at(result, "15")

    def test_no_anomaly_for_a_stable_seasonal_pattern(self):
        settings = Settings(anomaly_seasonal_period=4, anomaly_zscore_threshold=2.0)
        values = [10.0, 20.0, 30.0, 40.0] * 4
        result = detect_anomalies(_points(values), settings)
        assert "seasonal" not in {s.method.value for a in result.anomalies for s in a.signals}

    def test_single_prior_occurrence_falls_back_to_percent_change(self):
        settings = Settings(anomaly_seasonal_period=4, anomaly_percent_change_threshold=20.0)
        values = [10.0, 20.0, 30.0, 40.0, 10.0, 20.0, 30.0, 500.0]
        result = detect_anomalies(_points(values), settings)
        signal = next(
            s
            for a in result.anomalies
            for s in a.signals
            if s.method == AnomalyMethod.SEASONAL and a.period == "7"
        )
        assert "one prior occurrence" in signal.formula

    def test_insufficient_data_with_no_prior_occurrence_at_lag(self):
        settings = Settings(anomaly_seasonal_period=12)
        result = detect_anomalies(_points([1.0, 2.0, 3.0]), settings)
        assert any(
            "seasonal: no prior occurrence at lag 12" in r for r in result.insufficient_data_reasons
        )


class TestMultiMethodAndMetadata:
    def test_a_point_can_be_flagged_by_multiple_methods_at_once(self):
        settings = Settings(
            anomaly_percent_change_threshold=20.0,
            anomaly_iqr_multiplier=1.5,
            anomaly_rolling_window=2,
        )
        points = _points([10.0, 11.0, 9.0, 500.0])
        result = detect_anomalies(points, settings)
        methods = _methods_at(result, "3")
        assert len(methods) >= 2

    def test_every_signal_carries_a_formula_and_threshold(self):
        settings = Settings(anomaly_percent_change_threshold=5.0)
        points = _points([10.0, 100.0])
        result = detect_anomalies(points, settings)
        for anomaly in result.anomalies:
            for signal in anomaly.signals:
                assert signal.formula
                assert signal.threshold_used is not None

    def test_engine_version_is_stamped(self):
        from analytics.models import ANALYTICS_ENGINE_VERSION

        result = detect_anomalies(_points([1.0, 2.0, 3.0]))
        assert result.engine_version == ANALYTICS_ENGINE_VERSION

    def test_empty_series(self):
        result = detect_anomalies([])
        assert result.anomalies == ()
