"""Deterministic forecasting over a chronological series -- Prompt 16
(`16_FORECASTING_CONTRACT.md`).

`generate_forecast` extends `analytics.engine.compute_analytics_result`'s
`TIME_SERIES` branch (and the `GrowthStat.points` series it already
computes -- zero new queries, zero new data) one step further: not just
describing the observed history, but projecting it forward. Five
deterministic, stdlib-only baseline models (naive, seasonal-naive, moving
average, linear trend, simple exponential smoothing) plus an `AUTO` mode
that backtests every eligible one and picks the most accurate. Pure, no
LLM, no I/O -- the same posture every other module under `analytics/`
already establishes (`engine.py`/`anomaly.py`/`root_cause.py`/
`visualization.py`), and, like `engine.py`'s own percentile/variance
computations, deliberately uses only the `statistics`/`math` standard
library rather than adding numpy/pandas-time-series/statsmodels/sklearn to
`requirements.txt` -- this repository has no general-purpose ML/stats
dependency today, and a handful of closed-form baseline models don't
justify adding one.

**A forecast is never a `DataTruthLevel.DATABASE_FACT`.** Every
`analytics.models.ForecastResult` this module produces is tagged
`DataTruthLevel.AI_INFERENCE` (`ForecastResult.truth_level`) -- a
deliberate, disclosed extension of that enum's literal docstring ("a
claim an LLM generated"): nothing here calls an LLM, but a forecast is
just as surely *not* a fact read straight from executed rows -- it is a
statistical extrapolation beyond them, carrying real, stated uncertainty.
Per master-contract rules 9-10, it must never be presented as settled fact
or silently promoted to `CONFIRMED_BUSINESS_TRUTH`; every `"ok"`
`ForecastResult` therefore also carries a non-empty `limitations` tuple
and a human-readable `summary` that says "forecast"/"estimate" plainly,
never phrased as if it were an already-known value.

**Rejects rather than guesses** when the input is insufficient or its
temporal semantics can't be trusted (Prompt 16's own "reject forecasts
when data is insufficient or semantics are untrusted" requirement): fewer
than `Settings.forecast_min_data_points` historical points, a label column
`analytics.engine.classify_period_column` can't confidently classify as
chronological at all, or one it classifies as a raw ISO date (a
recognized *shape* for `TIME_SERIES` classification, but deliberately
**not** forecastable here -- no reliable reporting cadence, daily? weekly?
month-end snapshot?, can be inferred from the label alone, and guessing
wrong would synthesize a confidently incorrect future label; see
`analytics.engine.next_period_label`'s own identical, pre-existing
"date gets no gap detection" scope boundary -- forecasting inherits that
same boundary rather than relaxing it). A rejection is a normal, typed
`ForecastResult(status="rejected", ...)`, never an exception -- the same
fail-open-to-the-caller posture `analytics.engine.compute_analytics_result`
and `agent.nodes.compute_analytics_node` already establish; nothing here
is ever a reason the overall `/ask` run fails.
"""

from __future__ import annotations

import math
import statistics as _statistics
from collections.abc import Callable
from dataclasses import dataclass, field

from recommendation.models import Recommendation, RecommendationKind

from agent.provenance import DataTruthLevel, ProvenancedClaim
from analytics.engine import classify_period_column, next_period_label
from analytics.models import (
    ForecastEvaluation,
    ForecastModelMetadata,
    ForecastModelType,
    ForecastPoint,
    ForecastResult,
)
from config.settings import Settings, get_settings

_SOURCE = "analytics.forecasting.generate_forecast"

#: Common confidence levels with a precomputed two-sided normal
#: z-multiplier -- deliberately a small lookup table rather than an
#: inverse-normal-CDF implementation (no scipy, matching this module's own
#: "stdlib only" posture). An unlisted `Settings.forecast_confidence_level`
#: falls back to the 95% value and is disclosed via `ForecastResult
#: .limitations` (see `_build_limitations`), never silently misreported.
_Z_BY_CONFIDENCE: dict[float, float] = {
    0.80: 1.2816,
    0.90: 1.6449,
    0.95: 1.9600,
    0.98: 2.3263,
    0.99: 2.5758,
}
_DEFAULT_CONFIDENCE_Z = 1.9600

#: Models tried, in this priority order, both for `AUTO` backtest
#: comparison and as the fallback order when no candidate has enough
#: history for a backtest at all (see `_select_best_model`).
_CANDIDATE_ORDER: tuple[ForecastModelType, ...] = (
    ForecastModelType.LINEAR_TREND,
    ForecastModelType.SEASONAL_NAIVE,
    ForecastModelType.SIMPLE_EXPONENTIAL_SMOOTHING,
    ForecastModelType.MOVING_AVERAGE,
    ForecastModelType.NAIVE,
)

#: A backtest MAPE above this is called out in `recommendations_for_forecast`
#: as "treat this forecast as directional, not precise" -- a disclosed,
#: round-number threshold, not a statistically derived one (matching
#: `analytics.root_cause`'s own disclosed, round-number
#: `root_cause_min_contribution_percent` default's honesty).
_HIGH_MAPE_PERCENT = 25.0


@dataclass(frozen=True)
class _FittedModel:
    """One model's internal fit over a training series -- never exposed
    outside this module (`ForecastModelMetadata` is the public, typed
    equivalent `generate_forecast` constructs from this)."""

    forecast_fn: Callable[[int], float]
    #: Population stddev of the model's own in-sample one-step-ahead
    #: residuals -- `None` when there weren't at least 2 residuals to
    #: compute one from, or exactly `0.0` when the model fit the training
    #: data with no residual variation at all (e.g. a perfectly linear
    #: series). Both are treated identically downstream as "no usable
    #: interval" (an interval-less point forecast is still a valid,
    #: disclosed result -- see `ForecastModelMetadata.supports_interval`) --
    #: a zero-width interval would otherwise overstate certainty.
    sigma: float | None
    #: Returns the multiplier `k` such that a horizon-`h` forecast's
    #: prediction-interval half-width is `z * sigma * k(h)` -- isolates
    #: each model's own error-growth shape from the shared z-multiplier/
    #: sigma scaling `_build_points` applies uniformly.
    interval_width_fn: Callable[[int], float]
    parameters: dict[str, float | int | str | None] = field(default_factory=dict)


def _z_for_confidence(level: float) -> tuple[float, bool]:
    """Returns `(z, matched)` -- `matched` is `False` when `level` isn't
    one of `_Z_BY_CONFIDENCE`'s keys (within floating-point tolerance) and
    the 95% default was used instead."""
    for known_level, z in _Z_BY_CONFIDENCE.items():
        if abs(known_level - level) < 1e-9:
            return z, True
    return _DEFAULT_CONFIDENCE_Z, False


# ---------------------------------------------------------------------------
# Per-model fitting -- each returns `None` when the training series is too
# short for that specific model, never raises.
# ---------------------------------------------------------------------------


def _fit_naive(values: list[float], settings: Settings) -> _FittedModel | None:
    n = len(values)
    if n < 2:
        return None
    residuals = [values[i] - values[i - 1] for i in range(1, n)]
    sigma = _statistics.pstdev(residuals) if len(residuals) >= 2 else None
    last = values[-1]
    return _FittedModel(
        forecast_fn=lambda h: last,
        sigma=sigma,
        interval_width_fn=lambda h: math.sqrt(h),
        parameters={},
    )


def _fit_moving_average(values: list[float], settings: Settings) -> _FittedModel | None:
    window = min(settings.forecast_moving_average_window, len(values))
    n = len(values)
    if window < 1 or n < window + 1:
        return None

    def _average_ending_at(end_idx: int) -> float:
        return sum(values[end_idx - window : end_idx]) / window

    residuals = [values[i] - _average_ending_at(i) for i in range(window, n)]
    sigma = _statistics.pstdev(residuals) if len(residuals) >= 2 else None
    level = sum(values[-window:]) / window
    return _FittedModel(
        forecast_fn=lambda h: level,
        sigma=sigma,
        interval_width_fn=lambda h: math.sqrt(h),
        parameters={"window": window},
    )


def _fit_seasonal_naive(
    values: list[float], settings: Settings, seasonal_period: int | None
) -> _FittedModel | None:
    m = seasonal_period
    n = len(values)
    if not m or m < 2 or n <= m:
        return None
    residuals = [values[i] - values[i - m] for i in range(m, n)]
    sigma = _statistics.pstdev(residuals) if len(residuals) >= 2 else None

    def _forecast(h: int) -> float:
        idx = n - m + ((h - 1) % m)
        return values[idx]

    return _FittedModel(
        forecast_fn=_forecast,
        sigma=sigma,
        interval_width_fn=lambda h: math.sqrt(((h - 1) // m) + 1),
        parameters={"seasonal_period": m},
    )


def _fit_linear_trend(values: list[float], settings: Settings) -> _FittedModel | None:
    n = len(values)
    if n < 3:
        return None
    t = list(range(n))
    mean_t = sum(t) / n
    mean_y = sum(values) / n
    ss_t = sum((ti - mean_t) ** 2 for ti in t)
    if ss_t == 0:
        return None
    slope = sum((ti - mean_t) * (yi - mean_y) for ti, yi in zip(t, values, strict=False)) / ss_t
    intercept = mean_y - slope * mean_t
    residuals = [values[i] - (intercept + slope * t[i]) for i in range(n)]
    degrees_of_freedom = n - 2
    sse = sum(r * r for r in residuals)
    residual_std_error = math.sqrt(sse / degrees_of_freedom) if degrees_of_freedom > 0 else None

    def _forecast(h: int) -> float:
        return intercept + slope * (n - 1 + h)

    def _width(h: int) -> float:
        x0 = n - 1 + h
        return math.sqrt(1 + 1 / n + ((x0 - mean_t) ** 2) / ss_t)

    return _FittedModel(
        forecast_fn=_forecast,
        sigma=residual_std_error,
        interval_width_fn=_width,
        parameters={"slope": round(slope, 6), "intercept": round(intercept, 6)},
    )


def _fit_ses(values: list[float], settings: Settings) -> _FittedModel | None:
    n = len(values)
    if n < 2:
        return None
    alpha = settings.forecast_ses_alpha
    level = values[0]
    residuals: list[float] = []
    for i in range(1, n):
        residuals.append(values[i] - level)
        level = alpha * values[i] + (1 - alpha) * level
    sigma = _statistics.pstdev(residuals) if len(residuals) >= 2 else None
    final_level = level

    def _width(h: int) -> float:
        # Classic SES h-step-ahead forecast-error variance (Hyndman &
        # Athanasopoulos): var(h) = sigma^2 * (1 + (h-1)*alpha^2).
        return math.sqrt(1 + (h - 1) * (alpha**2))

    return _FittedModel(
        forecast_fn=lambda h: final_level,
        sigma=sigma,
        interval_width_fn=_width,
        parameters={"alpha": alpha},
    )


def _fit(
    model_type: ForecastModelType,
    values: list[float],
    settings: Settings,
    seasonal_period: int | None,
) -> _FittedModel | None:
    """Dispatches to one model's own fitter. Never called with `AUTO` --
    `generate_forecast` always resolves `AUTO` to a concrete type first
    (see `_select_best_model`)."""
    if model_type == ForecastModelType.NAIVE:
        return _fit_naive(values, settings)
    if model_type == ForecastModelType.MOVING_AVERAGE:
        return _fit_moving_average(values, settings)
    if model_type == ForecastModelType.SEASONAL_NAIVE:
        return _fit_seasonal_naive(values, settings, seasonal_period)
    if model_type == ForecastModelType.LINEAR_TREND:
        return _fit_linear_trend(values, settings)
    if model_type == ForecastModelType.SIMPLE_EXPONENTIAL_SMOOTHING:
        return _fit_ses(values, settings)
    raise ValueError(f"Unsupported forecast model type for fitting: {model_type!r}")


# ---------------------------------------------------------------------------
# Evaluation (backtest) + AUTO model selection
# ---------------------------------------------------------------------------


def _backtest(
    model_type: ForecastModelType,
    values: list[float],
    settings: Settings,
    seasonal_period: int | None,
) -> ForecastEvaluation | None:
    """Holds out the last `Settings.forecast_backtest_holdout` points,
    fits `model_type` on everything before them, and compares its
    forecast for those held-out steps against their real values. `None`
    when there isn't enough history for a holdout of that size, or the
    model itself can't be fit on the (shorter) training slice -- never
    raises.
    """
    holdout = settings.forecast_backtest_holdout
    n = len(values)
    if holdout < 1 or n <= holdout:
        return None
    train = values[:-holdout]
    actual = values[-holdout:]
    fitted = _fit(model_type, train, settings, seasonal_period)
    if fitted is None:
        return None
    predicted = [fitted.forecast_fn(h) for h in range(1, holdout + 1)]
    errors = [a - p for a, p in zip(actual, predicted, strict=False)]
    mae = sum(abs(e) for e in errors) / holdout
    rmse = math.sqrt(sum(e * e for e in errors) / holdout)
    pct_errors = [abs(e) / abs(a) for e, a in zip(errors, actual, strict=False) if a != 0]
    mape = round(100 * sum(pct_errors) / len(pct_errors), 2) if pct_errors else None
    return ForecastEvaluation(
        holdout_size=holdout, mae=round(mae, 4), rmse=round(rmse, 4), mape=mape
    )


def _select_best_model(
    values: list[float], settings: Settings, seasonal_period: int | None
) -> tuple[ForecastModelType, dict[str, ForecastEvaluation]]:
    """`ForecastModelType.AUTO`'s own selection: backtests every candidate
    in `_CANDIDATE_ORDER` and picks the lowest-MAPE one (falling back to
    RMSE when MAPE is undefined for a candidate -- see
    `ForecastEvaluation.mape`'s own zero-denominator note). When *no*
    candidate has enough history for a backtest at all, falls back to the
    first candidate (in the same priority order) that can still be
    *fit* without an evaluation -- so `AUTO` never fails purely because
    the series is too short to backtest; it can still forecast, just
    without evaluation-based selection (disclosed via `ForecastResult
    .limitations`).
    """
    evaluations: dict[str, ForecastEvaluation] = {}
    for candidate in _CANDIDATE_ORDER:
        evaluation = _backtest(candidate, values, settings, seasonal_period)
        if evaluation is not None:
            evaluations[candidate.value] = evaluation

    if evaluations:

        def _rank_key(item: tuple[str, ForecastEvaluation]) -> float:
            _name, evaluation = item
            return evaluation.mape if evaluation.mape is not None else evaluation.rmse

        best_name = min(evaluations.items(), key=_rank_key)[0]
        return ForecastModelType(best_name), evaluations

    for candidate in _CANDIDATE_ORDER:
        if _fit(candidate, values, settings, seasonal_period) is not None:
            return candidate, evaluations
    return ForecastModelType.NAIVE, evaluations


# ---------------------------------------------------------------------------
# Future-period labeling + point assembly
# ---------------------------------------------------------------------------


def _period_label_generator(period_kind: str, last_label: str) -> Callable[[int], str]:
    """Returns a function mapping a horizon step `h` (1-indexed) to its
    future period label, by repeatedly applying `analytics.engine
    .next_period_label` -- `period_kind` is guaranteed `"year"`/
    `"year_month"` by the time this is called (see `generate_forecast`'s
    own rejection of `None`/`"date"` period kinds), so every call is
    guaranteed non-`None`.
    """
    cache: dict[int, str] = {0: last_label}

    def _label(h: int) -> str:
        if h in cache:
            return cache[h]
        previous = _label(h - 1)
        current = next_period_label(period_kind, previous)
        if current is None:
            # 2026 Phase 3 security review precedent (bandit B101): an
            # explicit raise, not `assert` -- stripped under `python -O`.
            # Precondition violation, not a real runtime case: `period_kind`
            # is guaranteed "year"/"year_month" by generate_forecast's own
            # rejection of None/"date" period kinds before this is ever
            # called, so next_period_label always returns non-None for it.
            raise RuntimeError(
                f"next_period_label returned None for period_kind={period_kind!r}, "
                f"previous={previous!r} -- generate_forecast's own gate should have "
                "prevented this."
            )
        cache[h] = current
        return current

    return _label


def _build_points(
    fitted: _FittedModel, horizon: int, z: float, next_label: Callable[[int], str]
) -> tuple[ForecastPoint, ...]:
    points = []
    for h in range(1, horizon + 1):
        value = fitted.forecast_fn(h)
        lower: float | None = None
        upper: float | None = None
        if fitted.sigma:  # None or exactly 0.0 both mean "no usable interval" -- see _fit*'s own
            # docstrings: a zero in-sample residual stddev would otherwise collapse to a
            # zero-width [point, point] "interval," which overstates certainty rather than
            # disclosing that none could be estimated (the same "a degenerate stat is worse than
            # none" posture analytics.anomaly's own `if stddev == 0: continue` guards establish).
            width = fitted.sigma * fitted.interval_width_fn(h)
            lower = value - z * width
            upper = value + z * width
        points.append(
            ForecastPoint(
                period=next_label(h),
                forecast=round(value, 4),
                lower_bound=round(lower, 4) if lower is not None else None,
                upper_bound=round(upper, 4) if upper is not None else None,
                horizon_step=h,
            )
        )
    return tuple(points)


def _build_limitations(
    model_type: ForecastModelType,
    fitted: _FittedModel,
    settings: Settings,
    confidence_matched: bool,
    evaluation: ForecastEvaluation | None,
) -> list[str]:
    """Always returns at least one entry -- the literal "forecasts are
    explicitly represented as estimates with limitations" acceptance
    criterion, enforced here rather than left to whichever caller renders
    this result."""
    limitations = [
        "This is a statistical estimate, not a guarantee -- it assumes the historical "
        "pattern continues unchanged and does not account for future events, "
        "promotions, policy changes, or other external factors (no exogenous "
        "variables are modeled)."
    ]
    if not fitted.sigma:
        limitations.append(
            "Not enough historical variation to estimate a prediction interval -- only "
            "a point forecast is provided."
        )
    if not confidence_matched:
        limitations.append(
            f"Settings.forecast_confidence_level={settings.forecast_confidence_level!r} has no "
            "exact z-multiplier configured for it; a 95% confidence z-value was used instead."
        )
    if evaluation is None:
        limitations.append(
            f"Not enough history for a {settings.forecast_backtest_holdout}-point backtest -- "
            "no evaluation (MAE/RMSE/MAPE) is available for this forecast."
        )
    if model_type == ForecastModelType.SEASONAL_NAIVE:
        limitations.append(
            "The seasonal-naive model repeats the most recently observed full season's "
            "pattern -- it does not separately model a trend on top of seasonality."
        )
    if model_type in (
        ForecastModelType.NAIVE,
        ForecastModelType.MOVING_AVERAGE,
        ForecastModelType.SIMPLE_EXPONENTIAL_SMOOTHING,
    ):
        limitations.append(
            f"The {model_type.value} model produces a flat forecast across the whole "
            "horizon -- it does not project a trend."
        )
    return limitations


def _build_summary(
    status: str,
    rejection_reasons: tuple[str, ...],
    model_type: ForecastModelType | None,
    points: tuple[ForecastPoint, ...],
) -> str:
    if status == "rejected":
        return "Forecast not available: " + "; ".join(rejection_reasons) + "."
    first, last = points[0], points[-1]
    if len(points) == 1:
        return (
            f"Estimated {first.period} at {first.forecast:g} using the "
            f"{model_type.value if model_type else 'unknown'} model (a projection, not a "
            "confirmed value)."
        )
    return (
        f"Estimated {first.period} to {last.period} using the "
        f"{model_type.value if model_type else 'unknown'} model, ending at approximately "
        f"{last.forecast:g} (a projection, not a confirmed value)."
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_forecast(
    points: list[tuple[str, float]],
    horizon: int | None = None,
    model_type: ForecastModelType = ForecastModelType.AUTO,
    settings: Settings | None = None,
) -> ForecastResult:
    """Forecasts `horizon` future periods beyond `points`, a chronological
    `(period, value)` series -- the same shape `analytics.anomaly
    .detect_anomalies` takes, and typically the exact `GrowthStat.points`
    series `analytics.engine.compute_analytics_result`'s `TIME_SERIES`
    branch already computed for the same result (zero new queries).

    Args:
        points: The historical series, oldest first.
        horizon: How many future periods to forecast. Defaults to
            `Settings.forecast_default_horizon`. Values outside
            `[1, Settings.forecast_max_horizon]` are rejected (see
            `ForecastResult.rejection_reasons`), never silently clamped.
        model_type: Which model to use, or `ForecastModelType.AUTO` (the
            default) to backtest every eligible candidate and pick the
            most accurate -- see `_select_best_model`.
        settings: Defaults to `config.settings.get_settings()`.

    Returns:
        A `ForecastResult` -- `status="rejected"` (with every reason, never
        just the first) when the data is insufficient or its temporal
        semantics can't be trusted; `status="ok"` otherwise, always with a
        non-empty `limitations` tuple and `truth_level=AI_INFERENCE`. Never
        raises on a data-sufficiency problem; a genuinely unexpected error
        is the caller's (`agent.nodes.generate_forecast_node`'s) own
        fail-open responsibility, matching `compute_analytics_node`'s
        identical posture.
    """
    settings = settings or get_settings()
    effective_horizon = horizon if horizon is not None else settings.forecast_default_horizon

    rejections: list[str] = []
    if effective_horizon < 1:
        rejections.append("horizon must be at least 1")
    elif effective_horizon > settings.forecast_max_horizon:
        rejections.append(
            f"horizon {effective_horizon} exceeds the configured maximum of "
            f"{settings.forecast_max_horizon}"
        )

    labels = [p[0] for p in points]
    values = [p[1] for p in points]
    n = len(values)

    if n < settings.forecast_min_data_points:
        rejections.append(
            f"fewer than {settings.forecast_min_data_points} historical data point(s) (got {n})"
        )

    period_kind = classify_period_column(labels) if labels else None
    if period_kind is None:
        rejections.append(
            "the historical series' label column is not confidently chronological "
            "(untrusted temporal semantics) -- forecasting requires a majority-matching "
            "year or year-month label pattern"
        )
    elif period_kind == "date":
        rejections.append(
            "forecasting is not supported for a raw ISO-date-labeled series -- no reliable "
            "reporting cadence can be inferred from the label shape alone (only year and "
            "year-month series are supported)"
        )

    if rejections:
        return ForecastResult(
            status="rejected",
            rejection_reasons=tuple(rejections),
            horizon=effective_horizon,
            summary=_build_summary("rejected", tuple(rejections), None, ()),
        )

    seasonal_period = settings.forecast_seasonal_period
    if seasonal_period is None and period_kind == "year_month":
        seasonal_period = 12

    candidate_evaluations: dict[str, ForecastEvaluation] = {}
    if model_type == ForecastModelType.AUTO:
        chosen_type, candidate_evaluations = _select_best_model(values, settings, seasonal_period)
    else:
        chosen_type = model_type

    fitted = _fit(chosen_type, values, settings, seasonal_period)
    if fitted is None:
        reason = f"insufficient history for the {chosen_type.value} model to produce a forecast"
        return ForecastResult(
            status="rejected",
            rejection_reasons=(reason,),
            horizon=effective_horizon,
            summary=_build_summary("rejected", (reason,), None, ()),
        )

    z, confidence_matched = _z_for_confidence(settings.forecast_confidence_level)
    # period_kind is guaranteed "year"/"year_month" here (the `None`/"date"
    # cases both appended to `rejections` above and returned already) -- mypy
    # can't trace that correlation through the rejections-list pattern, the
    # identical narrowing gap `period_kind=period_kind,  # type: ignore[arg-type]`
    # below already has for the same reason.
    next_label = _period_label_generator(period_kind, labels[-1])  # type: ignore[arg-type]
    forecast_points = _build_points(fitted, effective_horizon, z, next_label)
    evaluation = _backtest(chosen_type, values, settings, seasonal_period)
    limitations = _build_limitations(chosen_type, fitted, settings, confidence_matched, evaluation)

    metadata = ForecastModelMetadata(
        model=chosen_type,
        parameters=fitted.parameters,
        training_window_start=labels[0],
        training_window_end=labels[-1],
        training_point_count=n,
        period_kind=period_kind,  # type: ignore[arg-type]
        supports_interval=bool(fitted.sigma),
        confidence_level=settings.forecast_confidence_level,
    )

    return ForecastResult(
        status="ok",
        horizon=effective_horizon,
        model=metadata,
        points=forecast_points,
        evaluation=evaluation,
        candidate_evaluations=candidate_evaluations,
        limitations=tuple(limitations),
        summary=_build_summary("ok", (), chosen_type, forecast_points),
    )


def recommendations_for_forecast(result: ForecastResult) -> tuple[Recommendation, ...]:
    """Builds `recommendation.models.Recommendation`s from an already-
    computed `ForecastResult` -- Prompt 16's own "integrate with the
    recommendation contract" requirement.

    Deliberately a standalone function, not a `recommendation.provider
    .RecommendationProvider` implementation: that Protocol's own docstring
    already discloses its `recommend(summary: ResultSummary)` signature is
    not yet fixed for a producer whose input isn't a `ResultSummary` (a
    forecast's input is a `ForecastResult`, not a query-result summary).
    This function reuses the exact same typed `Recommendation`/
    `RecommendationKind` contracts that Protocol's own implementations
    would, rather than inventing a parallel shape.

    Every `Recommendation` here is `DataTruthLevel.AI_INFERENCE` (enforced
    by `Recommendation`'s own validator) -- never a claim about the
    forecast is promoted further than that.
    """
    recommendations: list[Recommendation] = []

    if result.status == "rejected":
        recommendations.append(
            Recommendation(
                kind=RecommendationKind.ACTION,
                claim=ProvenancedClaim(
                    value=(
                        "This result could not be forecast ("
                        + "; ".join(result.rejection_reasons)
                        + "). Consider asking for more historical periods, or a question "
                        "whose result breaks down by year or month."
                    ),
                    level=DataTruthLevel.AI_INFERENCE,
                    source=_SOURCE,
                ),
                rationale="analytics.forecasting.generate_forecast rejected this series.",
            )
        )
        return tuple(recommendations)

    if (
        result.evaluation is not None
        and result.evaluation.mape is not None
        and result.evaluation.mape > _HIGH_MAPE_PERCENT
    ):
        recommendations.append(
            Recommendation(
                kind=RecommendationKind.ACTION,
                claim=ProvenancedClaim(
                    value=(
                        f"This forecast's backtest error (MAPE) is {result.evaluation.mape:g}%, "
                        "higher than usual -- treat the forecasted values as directional "
                        "rather than precise."
                    ),
                    level=DataTruthLevel.AI_INFERENCE,
                    source=_SOURCE,
                ),
                rationale="High backtest MAPE on held-out historical data.",
            )
        )

    if result.model is not None and not result.model.supports_interval:
        recommendations.append(
            Recommendation(
                kind=RecommendationKind.NEXT_QUESTION,
                claim=ProvenancedClaim(
                    value=(
                        "Ask about a longer history window to get a prediction interval, "
                        "not just a point forecast."
                    ),
                    level=DataTruthLevel.AI_INFERENCE,
                    source=_SOURCE,
                ),
                rationale="No prediction interval could be estimated from the available history.",
            )
        )

    return tuple(recommendations)
