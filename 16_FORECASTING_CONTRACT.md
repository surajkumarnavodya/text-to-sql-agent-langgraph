# 16 — Deterministic Forecasting

Prompt 16 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`15_VISUALIZATION_ENGINE_CONTRACT.md`. One new backend module
(`analytics/forecasting.py`), additive `agent/state.py`/`agent/graph.py`/
`agent/nodes.py` wiring (a new, straight-edge graph node — never a
conditional one, never able to short-circuit the graph), additive
`config.settings`/`api/schemas.py`/`api/main.py` surface, and additive
`agent/orchestrator/` threading so the new per-request `forecast_horizon`
reaches `run_agent` on both the direct and orchestrated call paths. No
existing node, route, or response field changed shape; every new field is
optional/defaulted, and every caller that predates this prompt keeps
working unchanged.

## 1. Inspection: what already existed vs. the real gap

`analytics/engine.py`'s `compute_analytics_result` already classified a
`TIME_SERIES`-shaped result and computed a full `GrowthStat` — period-by-
period values, growth percentages, and missing-period detection
(`detect_missing_periods`, itself promoted from a private
`_detect_missing_periods` for this prompt to reuse, the same "promote for
cross-module reuse" precedent `group_and_sum_by_label` already established
for Prompt 14). `agent.intent.AnalyticalIntentType.FORECAST` already
existed as one of Prompt 11's 13 classified intent categories — nothing
in this codebase, until now, actually *acted* on a question being
classified that way; it was recorded and otherwise ignored. There was no
mapping anywhere from "here is a historical series" to "here is a
projected continuation of it" — grep confirmed zero existing forecasting/
extrapolation logic in this codebase before this prompt.

**The one real design decision, made explicitly rather than assumed:**
this repository has no general-purpose ML/stats dependency today (no
numpy, pandas-time-series, statsmodels, or scikit-learn) and `analytics/`'s
own established posture (`engine.py`'s percentile/variance math,
`anomaly.py`'s z-score/IQR detection) is stdlib-only, deterministic, no
I/O. A handful of closed-form baseline forecasting models — naive,
seasonal-naive, moving average, linear trend, simple exponential
smoothing — don't justify breaking that posture by adding a new
dependency, so `analytics/forecasting.py` implements all five directly
against `statistics`/`math` alone, plus an `AUTO` mode that backtests
every eligible candidate and picks the most accurate.

## 2. What was built

### `analytics/forecasting.py` (new)

`generate_forecast(points, horizon=None, model_type=ForecastModelType.AUTO,
settings=None) -> ForecastResult` — pure, deterministic, no LLM, no I/O,
the identical posture every other `analytics/` module already establishes.

1. **Input is the exact `GrowthStat.points` series** `compute_analytics_result`'s
   `TIME_SERIES` branch already computes — zero new queries, zero new
   data collection. `agent.nodes.generate_forecast_node`'s own
   `_extract_growth_series` helper pulls it back out of the already-stored
   `AgentState["analytical_result"]` dict.
2. **Five fitted models** (`_fit_naive`/`_fit_moving_average`/
   `_fit_seasonal_naive`/`_fit_linear_trend`/`_fit_ses`), each returning
   `None` (never raising) when the training series is too short for that
   specific model. Each records its own in-sample residual standard
   deviation (for a prediction interval) and an `interval_width_fn`
   isolating its own error-growth shape (flat for naive/moving-average,
   `sqrt(h)`-scaled for seasonal-naive, a real leverage-adjusted width for
   linear trend, the classical Hyndman & Athanasopoulos SES h-step-ahead
   variance formula for SES).
3. **`AUTO` model selection** (`_select_best_model`): backtests every
   candidate in a fixed priority order, holding out the last
   `Settings.forecast_backtest_holdout` points, and picks the lowest-MAPE
   one (falling back to RMSE when MAPE is undefined — every held-out
   actual was zero). When *no* candidate has enough history to backtest at
   all, falls back to the first candidate that can still be *fit* without
   an evaluation, so `AUTO` never fails purely for being too short to
   backtest — it can still forecast, just without evaluation-based
   selection (disclosed via `limitations`).
4. **Rejects rather than guesses** (the literal "reject forecasts when
   data is insufficient or semantics are untrusted" requirement): fewer
   than `Settings.forecast_min_data_points` historical points, a label
   column `classify_period_column` can't confidently classify as
   chronological, or one classified as a raw ISO date (a recognized
   `TIME_SERIES` shape, but deliberately **not** forecastable — no
   reliable cadence is inferable from a bare date label, the identical
   scope boundary `detect_missing_periods` already draws for gap
   detection) all collect into a `rejection_reasons` tuple and return a
   normal, typed `ForecastResult(status="rejected", ...)` — never an
   exception, and every reason is reported, not just the first.
5. **Prediction intervals**, where supported: a small lookup table of
   two-sided normal z-multipliers for the common confidence levels
   (80/90/95/98/99%) — no scipy inverse-normal-CDF dependency. An unlisted
   `Settings.forecast_confidence_level` falls back to the 95% value and is
   disclosed via `limitations`, never silently misreported. A model whose
   in-sample residual stddev is `None` or exactly `0.0` gets a point
   forecast only — a zero-width "interval" would overstate certainty
   rather than honestly disclosing that none could be estimated.
6. **Backtest evaluation** (`ForecastEvaluation`: MAE/RMSE/MAPE, formula
   recorded on the object itself), computed for whichever model actually
   produced the result (not only for `AUTO`'s own candidate comparison).
7. **`limitations` is always non-empty for an `"ok"` result** — the
   literal "forecasts are explicitly represented as estimates with
   limitations" acceptance criterion, enforced inside
   `_build_limitations` itself rather than left to whichever caller
   renders the result. Always at least the standing "this assumes the
   historical pattern continues unchanged, no exogenous variables"
   disclosure, plus model-specific ones (flat-forecast models disclose
   they don't project a trend; seasonal-naive discloses it doesn't
   separately model trend on top of seasonality; a missing interval or
   backtest is disclosed by name).
8. **Always `DataTruthLevel.AI_INFERENCE`** (`ForecastResult.truth_level`)
   — a deliberate, disclosed extension of that enum's literal docstring
   ("a claim an LLM generated"): nothing here calls an LLM, but a
   forecast is just as surely not a fact read from executed rows. Per
   master-contract rules 9–10, never silently promotable to
   `CONFIRMED_BUSINESS_TRUTH` — there is no promotion path in this
   codebase for a forecast, matching Prompt 07's identical "no promotion
   path exists" posture for inferred relationships.
9. **`recommendations_for_forecast(result)`** — the prompt's own
   "integrate with the recommendation contract" requirement, and the
   *first real producer* of `recommendation.models.Recommendation`
   anywhere in this codebase. Deliberately a standalone function, not an
   implementation of `recommendation.provider.RecommendationProvider`
   (that Protocol's own docstring already discloses its `recommend(summary:
   ResultSummary)` signature doesn't fit a producer whose input is a
   `ForecastResult`, not a `ResultSummary`) — reuses the exact typed
   `Recommendation`/`RecommendationKind` shape that Protocol's own future
   implementations would, rather than inventing a parallel one. Flags a
   high-MAPE backtest ("treat this forecast as directional, not
   precise") and a missing prediction interval (suggests asking for more
   history); for a rejected result, recommends what would make forecasting
   possible. **Not called from the live `/ask` path in this pass** — built,
   tested, and callable directly, the same "compute + test, defer the
   deepest wiring" posture `analytics.root_cause.investigate_root_cause`
   (Prompt 14) and `build_chart_spec`'s own `visualization_spec` (Prompt
   15) already established.

### `analytics/engine.py` (two helpers promoted to public)

`_expected_next_period`/`_detect_missing_periods` → `next_period_label`/
`detect_missing_periods`, with no behavior change — promoted purely so
`analytics.forecasting` can reuse the exact same period-arithmetic and
gap-detection logic rather than a second implementation. Every existing
internal call site updated to the new names; `tests/test_analytics_engine.py`
(pre-existing, unmodified) reran clean, confirming no behavior drift.

### `agent/state.py`, `agent/graph.py`, `agent/nodes.py` (additive wiring)

- `AgentState["forecast_horizon"]` (`int | None`, input, set once by
  `run_agent()` from the caller) and `AgentState["forecast_result"]`
  (`dict | None`, output) — the same plain-dict-of-a-`model_dump()`
  convention `analytical_result` already established in Prompt 13.
- `generate_forecast_node`, wired `compute_analytics → generate_forecast →
  generate_insight` — **a straight edge, never a conditional one**, so
  this node can never short-circuit the graph the way
  `classify_followup_node`'s "ambiguous" classification can (the identical
  structural proof pattern Prompts 11/12 already established for their own
  new nodes). Unlike `compute_analytics_node` right before it (which runs
  on *every* successful execution), this node only ever attempts a
  forecast when `classify_analytical_intent_node` already classified the
  question as `AnalyticalIntentType.FORECAST` — no new classification, no
  keyword regex, purely reusing a judgment already made earlier in the
  same run. `Settings.enable_forecasting` off, a non-FORECAST intent, or
  no extractable time-series growth data all skip the actual forecasting
  call, leaving `forecast_result` as `None` — never a reason the question
  itself fails. Fails open on any genuinely unexpected error, the same
  posture `compute_analytics_node` already establishes.
- `run_agent()` gained one new, defaulted keyword parameter
  (`forecast_horizon: int | None = None`) — every existing call site that
  predates this prompt keeps working unchanged; the graph now has
  seventeen nodes (see "Self-correcting retry loop" above for the
  full, current list).

### `config/settings.py` (8 new settings)

`enable_forecasting` (default `True` — a pure, zero-I/O computation, the
same "operator escape hatch, on by default" posture as
`enable_analytics_engine`/`enable_anomaly_detection`), plus seven
tunables feeding `analytics.forecasting` directly:
`forecast_default_horizon`/`forecast_max_horizon` (a hard, never-silently-
clamped cap — a request above it is rejected with HTTP 400),
`forecast_min_data_points`, `forecast_seasonal_period` (`None`/auto
resolves to 12 for a year-month series), `forecast_moving_average_window`,
`forecast_ses_alpha`, `forecast_backtest_holdout`, and
`forecast_confidence_level`.

### `api/schemas.py` / `api/main.py` (request/response + validation)

`AskRequest.forecast_horizon` (optional, `Field(gt=0)`) and a fully typed
`AskResponse.forecast_result: ForecastResultOut | None`
(`ForecastResultOut`/`ForecastModelMetadataOut`/`ForecastPointOut`/
`ForecastEvaluationOut` mirror `analytics.models`'s own typed shapes
directly — unlike `analytical_result`'s raw-dict shortcut, a forecast's
structure is small and client-relevant enough to warrant the fully typed
mirror, the same reasoning `VisualizationSpecOut` already applies).
`POST /ask` validates `forecast_horizon` against
`Settings.forecast_max_horizon` **before** any admission-control slot is
acquired or LLM/DB work starts — the identical "validate first" posture
`AskRequest.model`'s own allowlist check already establishes; an
out-of-range value is HTTP 400, not a wasted concurrency slot.
`_forecast_result_out` applies **no redaction** — unlike `insight`/
`rejection_message`, a forecast's text (`summary`/`limitations`/
`rejection_reasons`) is built entirely from this module's own fixed
strings and the question's already-validated historical period labels/
values, never from raw LLM or database-error text, so there is nothing
`security.redaction.redact_secrets` would ever need to catch here.

### `agent/orchestrator/graph.py` / `agent/orchestrator/nodes.py`

`run_orchestrated` gained the identical `forecast_horizon` parameter,
passed straight through to `run_agent` on the short-circuit (router-off)
path and threaded through `OrchestratorState["forecast_horizon"]` to
`sql_subgraph_node` on the multi-source path — no new routing logic,
no new source, just making sure a forecast-eligible question reaches the
SQL subgraph with its horizon intact regardless of which path answers it.

### `analytics/visualization.py` (additive)

`build_forecast_chart_spec(forecast_result, historical_points,
value_column_name, settings=None) -> ChartSpec | None` — a deterministic
chart specification for an already-computed `ForecastResult`, reusing the
exact `ChartSpec`/`ChartField`/`FieldRole`/`AccessibilityMetadata` typed
contracts Prompt 15's `build_chart_spec` already established (no new
chart-data shape). Always a `LINE` chart with two distinct `Y`/`SERIES`
fields (historical actual vs. forecasted estimate) — never one blended,
undifferentiated series — and discloses the estimate nature of the
forecasted portion via both `notices` (every `limitations` entry, plus
"periods after X are forecasted estimates") and `accessibility`
(`forecast_result.summary`, which itself always says "estimate"/
"forecast"). **Not consumed by `frontend/src/lib/chartEngine.ts` or
exposed on `ExecuteResponse` in this pass** — the identical "computed +
tested, not yet wired into the frontend" deferral `build_chart_spec`'s own
`visualization_spec` already established for Prompt 15; `forecast_result`
itself *is* already exposed, on `AskResponse` (see above), since
forecasting is reached through `/ask`'s own analytical-intent path, not
`/execute`'s raw-SQL-result path.

## 3. Testing

`tests/test_analytics_forecasting.py` (new, 360 lines): every model's
fitting behavior (including "too short to fit" → `None`, never an
exception), `AUTO` selection choosing the lowest-backtest-MAPE candidate,
rejection for insufficient data / untrusted temporal semantics / raw-date
labels, prediction-interval presence/absence, `limitations` always
non-empty for an `"ok"` result, deterministic reproducibility (the same
input always produces byte-identical output — no wall-clock timestamp
anywhere in `ForecastModelMetadata`, by design), and
`recommendations_for_forecast`'s own three cases (rejected, high-MAPE,
no-interval).

`tests/test_agent_nodes.py` (+137 lines) and
`tests/test_sql_agent_integration.py` (+41 lines) cover
`generate_forecast_node`'s own skip/attempt gates (flag off, non-FORECAST
intent, no extractable series, a genuine fitted result) and the graph's
new straight-edge wiring (`compute_analytics → generate_forecast →
generate_insight`, never a conditional routing table — the same
structural non-short-circuit proof Prompts 11/12 established for their
own new nodes).

**Found and fixed during this pass's own verification, not pre-existing:**
`run_agent`'s new `forecast_horizon` parameter broke 7 tests across
`tests/test_orchestrator.py` and `tests/test_api_ask.py` whose own
`fake_run_agent`/`_capture` stand-ins didn't accept the new keyword
argument — fixed by adding `forecast_horizon=None` to each stand-in's
signature (and capturing/asserting it where the test's own `captured`
dict already tracked every other parameter), not by loosening the stand-
ins to `**kwargs`. A `ruff`/`black`/`bandit` pass found and fixed one
genuinely unused import (`ForecastModelType` in `agent/nodes.py` —
`generate_forecast_node` never explicitly requests a model type, always
leaving `AUTO` as the default), one `assert`-based invariant converted to
an explicit `raise` (`_period_label_generator`'s own "period_kind is
guaranteed non-None/non-date by this point" guard — the identical bandit
B101 precedent the 2026 Phase 3 security review already established
elsewhere in this codebase, since `assert` is stripped under `python -O`),
and minor import-sort/line-length formatting in the new files. Full
regression suite reran clean after every fix: 3132/3132 backend tests
passing (up from 3088 before this prompt), 0 new mypy errors outside test
files, 0 bandit findings, 0 ruff findings, `black --check` clean.

## 4. Known, disclosed limitations (not oversights)

- **No exogenous variables, no detected seasonality beyond a fixed,
  operator-configured period** — every model assumes the historical
  pattern continues unchanged; a real regime change, promotion, or
  external shock is invisible to all five baseline models. Disclosed in
  every `"ok"` result's own `limitations`.
- **Only `"year"`/`"year_month"`-labeled series are forecastable** — a
  raw-ISO-date-labeled series is a recognized `TIME_SERIES` shape for
  *classification* purposes but is rejected for *forecasting* outright
  (no reliable cadence inferable from the label alone), inheriting the
  identical scope boundary `detect_missing_periods` already draws.
- **`recommendations_for_forecast` and `build_forecast_chart_spec` are
  not wired into the live `/ask`/`/execute` response paths in this
  pass** — both are fully built, fully tested, and callable directly
  (by a script, a future route, or a future pipeline extension), the
  same deferred-wiring posture this codebase has now established
  consistently across Prompts 14/15/16.
- **No frontend changes in this pass** — `AskResponse.forecast_result` is
  real, typed, API surface today; nothing in `frontend/src` reads it yet.
