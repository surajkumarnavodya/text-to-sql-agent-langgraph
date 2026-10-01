"""Unit tests for `analytics/forecasting.py` -- Prompt 16
(`16_FORECASTING_CONTRACT.md`)'s deterministic forecasting engine.

Covers: data-sufficiency/untrusted-semantics rejection, each baseline
model's point/interval output, `AUTO` model selection + candidate
evaluations, backtest evaluation (present and skipped-for-insufficient-
history), reproducibility, provenance (`AI_INFERENCE`, non-empty
`limitations`), and `recommendations_for_forecast`.
"""

from __future__ import annotations

from analytics.forecasting import generate_forecast, recommendations_for_forecast
from analytics.models import ForecastModelType
from recommendation.models import RecommendationKind

from agent.provenance import DataTruthLevel
from config.settings import Settings


def _year_points(values: list[float], start_year: int = 2015) -> list[tuple[str, float]]:
    return [(str(start_year + i), v) for i, v in enumerate(values)]


def _month_points(values: list[float], start: str = "2022-01") -> list[tuple[str, float]]:
    year, month = (int(p) for p in start.split("-"))
    points = []
    for v in values:
        points.append((f"{year:04d}-{month:02d}", v))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return points


class TestDataSufficiencyRejection:
    def test_rejects_fewer_than_min_data_points(self):
        settings = Settings(forecast_min_data_points=4)
        result = generate_forecast(_year_points([1.0, 2.0, 3.0]), settings=settings)
        assert result.status == "rejected"
        assert any("fewer than 4" in r for r in result.rejection_reasons)
        assert result.points == ()
        assert result.model is None

    def test_rejects_horizon_below_one(self):
        settings = Settings()
        result = generate_forecast(_year_points([1.0, 2.0, 3.0, 4.0]), horizon=0, settings=settings)
        assert result.status == "rejected"
        assert any("horizon must be at least 1" in r for r in result.rejection_reasons)

    def test_rejects_horizon_above_configured_max(self):
        settings = Settings(forecast_max_horizon=5)
        result = generate_forecast(_year_points([1.0, 2.0, 3.0, 4.0]), horizon=6, settings=settings)
        assert result.status == "rejected"
        assert any("exceeds the configured maximum" in r for r in result.rejection_reasons)

    def test_rejects_unrecognized_label_column_as_untrusted_semantics(self):
        settings = Settings()
        points = [("Q1", 10.0), ("Q2", 12.0), ("Q3", 15.0), ("Q4", 20.0)]
        result = generate_forecast(points, settings=settings)
        assert result.status == "rejected"
        assert any("not confidently chronological" in r for r in result.rejection_reasons)

    def test_rejects_raw_iso_date_series_as_unsupported_cadence(self):
        settings = Settings()
        points = [(f"2023-01-0{i}", 10.0 + i) for i in range(1, 6)]
        result = generate_forecast(points, settings=settings)
        assert result.status == "rejected"
        assert any("raw ISO-date-labeled series" in r for r in result.rejection_reasons)

    def test_rejects_when_explicit_model_cannot_be_fit(self):
        # SEASONAL_NAIVE with no seasonal_period configured and a yearly
        # series (no auto-default seasonal period for "year" data) can
        # never be fit.
        settings = Settings(forecast_min_data_points=4)
        result = generate_forecast(
            _year_points([1.0, 2.0, 3.0, 4.0, 5.0]),
            model_type=ForecastModelType.SEASONAL_NAIVE,
            settings=settings,
        )
        assert result.status == "rejected"
        assert any("seasonal_naive" in r for r in result.rejection_reasons)

    def test_accumulates_every_rejection_reason_not_just_the_first(self):
        settings = Settings(forecast_min_data_points=10, forecast_max_horizon=1)
        result = generate_forecast([("Q1", 1.0), ("Q2", 2.0)], horizon=5, settings=settings)
        assert result.status == "rejected"
        assert len(result.rejection_reasons) >= 2

    def test_rejection_has_a_non_empty_summary(self):
        result = generate_forecast(_year_points([1.0, 2.0]), settings=Settings())
        assert result.summary != ""
        assert "Forecast not available" in result.summary


class TestBaselineModels:
    def test_naive_repeats_the_last_value_flat(self):
        settings = Settings()
        points = _year_points([10.0, 12.0, 11.0, 13.0, 12.0])
        result = generate_forecast(
            points, horizon=3, model_type=ForecastModelType.NAIVE, settings=settings
        )
        assert result.status == "ok"
        assert result.model.model == ForecastModelType.NAIVE
        assert all(p.forecast == 12.0 for p in result.points)
        assert len(result.points) == 3

    def test_moving_average_uses_the_configured_window(self):
        settings = Settings(forecast_moving_average_window=2)
        points = _year_points([10.0, 20.0, 10.0, 20.0, 10.0, 20.0])
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.MOVING_AVERAGE, settings=settings
        )
        assert result.status == "ok"
        assert result.model.parameters["window"] == 2
        # Average of the last 2 values (10, 20) == 15
        assert result.points[0].forecast == 15.0

    def test_moving_average_window_capped_to_available_history(self):
        settings = Settings(forecast_moving_average_window=50)
        points = _year_points([10.0, 20.0, 30.0, 40.0])
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.MOVING_AVERAGE, settings=settings
        )
        assert result.status == "rejected"  # window + 1 > available points, even capped to n

    def test_seasonal_naive_repeats_the_prior_season(self):
        settings = Settings(forecast_seasonal_period=4)
        points = _year_points([1.0, 2.0, 3.0, 4.0, 1.5, 2.5, 3.5, 4.5])
        result = generate_forecast(
            points, horizon=4, model_type=ForecastModelType.SEASONAL_NAIVE, settings=settings
        )
        assert result.status == "ok"
        forecasts = [p.forecast for p in result.points]
        assert forecasts == [1.5, 2.5, 3.5, 4.5]

    def test_seasonal_naive_auto_defaults_to_12_for_year_month_series(self):
        settings = Settings(forecast_min_data_points=4)
        values = [float(i % 12) for i in range(24)]
        points = _month_points(values)
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.SEASONAL_NAIVE, settings=settings
        )
        assert result.status == "ok"
        assert result.model.parameters["seasonal_period"] == 12

    def test_linear_trend_projects_the_fitted_slope(self):
        settings = Settings()
        # Perfectly linear: y = 10 + 5x
        points = _year_points([10.0, 15.0, 20.0, 25.0, 30.0])
        result = generate_forecast(
            points, horizon=2, model_type=ForecastModelType.LINEAR_TREND, settings=settings
        )
        assert result.status == "ok"
        assert result.points[0].forecast == 35.0
        assert result.points[1].forecast == 40.0
        # A perfect line has ~zero residual error -- no meaningful interval
        # (a zero-width interval would overstate certainty, see
        # analytics.forecasting._build_points's own handling of sigma==0).
        assert result.points[0].lower_bound is None
        assert result.model.supports_interval is False
        assert any(
            "Not enough historical variation" in limitation for limitation in result.limitations
        )

    def test_simple_exponential_smoothing_uses_configured_alpha(self):
        settings = Settings(forecast_ses_alpha=1.0)
        points = _year_points([10.0, 20.0, 30.0, 40.0])
        result = generate_forecast(
            points,
            horizon=1,
            model_type=ForecastModelType.SIMPLE_EXPONENTIAL_SMOOTHING,
            settings=settings,
        )
        # alpha=1.0 collapses SES to the naive model (always tracks the
        # latest observed value exactly).
        assert result.status == "ok"
        assert result.points[0].forecast == 40.0
        assert result.model.parameters["alpha"] == 1.0

    def test_prediction_interval_widens_with_horizon_for_naive(self):
        settings = Settings()
        points = _year_points([10.0, 12.0, 9.0, 14.0, 11.0, 13.0])
        result = generate_forecast(
            points, horizon=3, model_type=ForecastModelType.NAIVE, settings=settings
        )
        widths = [
            (p.upper_bound - p.lower_bound) for p in result.points if p.upper_bound is not None
        ]
        assert widths == sorted(widths)
        assert widths[0] < widths[-1]

    def test_future_period_labels_extrapolate_year_month(self):
        settings = Settings()
        points = _month_points([1.0, 2.0, 3.0, 4.0, 5.0], start="2022-11")
        result = generate_forecast(
            points, horizon=3, model_type=ForecastModelType.NAIVE, settings=settings
        )
        labels = [p.period for p in result.points]
        assert labels == ["2023-04", "2023-05", "2023-06"]


class TestAutoModelSelection:
    def test_auto_picks_a_concrete_model_never_auto_itself(self):
        settings = Settings()
        points = _year_points([10.0, 15.0, 20.0, 25.0, 30.0, 35.0])
        result = generate_forecast(points, horizon=2, settings=settings)
        assert result.status == "ok"
        assert result.model.model != ForecastModelType.AUTO

    def test_auto_populates_candidate_evaluations(self):
        settings = Settings()
        points = _year_points([10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0])
        result = generate_forecast(points, horizon=2, settings=settings)
        assert result.status == "ok"
        assert len(result.candidate_evaluations) >= 2

    def test_explicit_model_selection_leaves_candidate_evaluations_empty(self):
        settings = Settings()
        points = _year_points([10.0, 15.0, 20.0, 25.0, 30.0])
        result = generate_forecast(
            points, horizon=2, model_type=ForecastModelType.NAIVE, settings=settings
        )
        assert result.candidate_evaluations == {}

    def test_auto_falls_back_when_no_candidate_has_enough_history_to_backtest(self):
        # 4 points, default backtest_holdout=2 -> train=2 points, which is
        # enough for NAIVE/LINEAR_TREND(needs 3, so no)/SES but not for a
        # meaningful ranked backtest across every candidate; AUTO must
        # still produce a forecast, just without evaluation-based ranking
        # for every candidate.
        settings = Settings(forecast_min_data_points=4, forecast_backtest_holdout=3)
        points = _year_points([10.0, 12.0, 14.0, 16.0])
        result = generate_forecast(points, horizon=1, settings=settings)
        assert result.status == "ok"
        assert result.model is not None


class TestEvaluation:
    def test_evaluation_present_with_enough_history(self):
        settings = Settings(forecast_backtest_holdout=2)
        points = _year_points([10.0, 12.0, 14.0, 16.0, 18.0, 20.0])
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.NAIVE, settings=settings
        )
        assert result.evaluation is not None
        assert result.evaluation.holdout_size == 2
        assert result.evaluation.mae >= 0
        assert result.evaluation.rmse >= 0

    def test_evaluation_skipped_and_disclosed_when_insufficient_history(self):
        settings = Settings(forecast_backtest_holdout=10, forecast_min_data_points=4)
        points = _year_points([10.0, 12.0, 14.0, 16.0])
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.NAIVE, settings=settings
        )
        assert result.status == "ok"
        assert result.evaluation is None
        assert any("backtest" in limitation for limitation in result.limitations)

    def test_mape_is_none_when_every_held_out_actual_is_zero(self):
        settings = Settings(forecast_backtest_holdout=2)
        points = _year_points([0.0, 0.0, 5.0, 0.0, 0.0])
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.NAIVE, settings=settings
        )
        assert result.evaluation is not None
        assert result.evaluation.mape is None


class TestProvenanceAndLimitations:
    def test_ok_result_is_always_ai_inference_with_non_empty_limitations(self):
        settings = Settings()
        points = _year_points([10.0, 15.0, 20.0, 25.0])
        result = generate_forecast(points, horizon=1, settings=settings)
        assert result.truth_level == DataTruthLevel.AI_INFERENCE
        assert len(result.limitations) >= 1

    def test_unmatched_confidence_level_is_disclosed(self):
        settings = Settings(forecast_confidence_level=0.87)
        points = _year_points([10.0, 15.0, 20.0, 25.0, 30.0])
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.NAIVE, settings=settings
        )
        assert any("no exact z-multiplier" in limitation for limitation in result.limitations)

    def test_ok_result_has_a_non_empty_summary_mentioning_estimate_language(self):
        settings = Settings()
        points = _year_points([10.0, 15.0, 20.0, 25.0])
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.NAIVE, settings=settings
        )
        assert "projection" in result.summary or "estimate" in result.summary.lower()


class TestReproducibility:
    def test_same_input_produces_byte_identical_output(self):
        settings = Settings()
        points = _year_points([10.0, 14.0, 9.0, 18.0, 13.0, 22.0])
        first = generate_forecast(points, horizon=3, settings=settings)
        second = generate_forecast(points, horizon=3, settings=settings)
        assert first.model_dump() == second.model_dump()

    def test_reproducible_for_every_explicit_model_type(self):
        settings = Settings(forecast_seasonal_period=3)
        points = _year_points([10.0, 14.0, 9.0, 18.0, 13.0, 22.0, 11.0, 19.0])
        for model_type in ForecastModelType:
            if model_type == ForecastModelType.AUTO:
                continue
            first = generate_forecast(points, horizon=2, model_type=model_type, settings=settings)
            second = generate_forecast(points, horizon=2, model_type=model_type, settings=settings)
            assert first.model_dump() == second.model_dump()

    def test_no_wall_clock_metadata_leaks_into_the_result(self):
        settings = Settings()
        points = _year_points([10.0, 15.0, 20.0, 25.0])
        result = generate_forecast(points, horizon=1, settings=settings)
        dumped = result.model_dump()
        assert "generated_at" not in dumped
        assert "timestamp" not in dumped
        if result.model is not None:
            assert "generated_at" not in result.model.model_dump()
            assert "timestamp" not in result.model.model_dump()


class TestRecommendationsForForecast:
    def test_rejected_forecast_yields_an_action_recommendation(self):
        result = generate_forecast(_year_points([1.0, 2.0]), settings=Settings())
        recommendations = recommendations_for_forecast(result)
        assert len(recommendations) == 1
        assert recommendations[0].kind == RecommendationKind.ACTION
        assert recommendations[0].claim.level == DataTruthLevel.AI_INFERENCE

    def test_high_backtest_mape_yields_an_action_recommendation(self):
        settings = Settings(forecast_backtest_holdout=2)
        # Erratic series -> poor backtest accuracy for a flat naive forecast.
        points = _year_points([10.0, 100.0, 5.0, 90.0, 8.0, 95.0])
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.NAIVE, settings=settings
        )
        recommendations = recommendations_for_forecast(result)
        assert any(r.kind == RecommendationKind.ACTION for r in recommendations)

    def test_no_interval_support_yields_a_next_question_recommendation(self):
        settings = Settings()
        # A perfectly linear series has zero residual error -> no interval.
        points = _year_points([10.0, 20.0, 30.0, 40.0])
        result = generate_forecast(
            points, horizon=1, model_type=ForecastModelType.LINEAR_TREND, settings=settings
        )
        assert result.model.supports_interval is False
        recommendations = recommendations_for_forecast(result)
        assert any(r.kind == RecommendationKind.NEXT_QUESTION for r in recommendations)

    def test_every_recommendation_claim_is_ai_inference(self):
        result = generate_forecast(_year_points([1.0, 2.0]), settings=Settings())
        for recommendation in recommendations_for_forecast(result):
            assert recommendation.claim.level == DataTruthLevel.AI_INFERENCE
