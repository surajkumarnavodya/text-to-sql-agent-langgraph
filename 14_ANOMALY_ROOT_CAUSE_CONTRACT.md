# 14 — Anomaly Detection & Evidence-Based Root Cause

Prompt 14 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/TEST/
REVIEW/FIX/DOCUMENT** — two new computation modules, one small promotion
of an existing private helper to public for reuse, one minimal-diff
integration into the already-live `analytics.engine.compute_analytics_result`,
and additive `config.settings` surface. No existing route, node, prompt,
or `Settings` field changed behavior with `enable_anomaly_detection` off,
and `agent/insight.py`/`analytics/provider.py` were not touched at all.

## 1. Inspection: what already existed vs. the real gap

`analytics/engine.py` (Prompt 13) already computes a full `GrowthStat`
series for a `TIME_SERIES` result and z-score/IQR outlier detection over
a *categorical* breakdown's static totals — but nothing in the codebase
scans a **chronological** series for anomalies against a *rolling* or
*seasonal* baseline; the existing outlier check has no notion of time
order at all. Nothing computes a dimension-level contribution/root-cause
breakdown between two periods either — `semantic/metrics.py` governs
metric *definitions*, not calculations; `eval/metrics.py::_percentile`
is an eval-harness-only latency helper; `agent.complexity`'s regex
signals detect *question phrasing* ("growth," "year-over-year"), never
compute anything. Exhaustive grep confirmed: no existing rolling-window,
seasonal-comparison, or cross-period-contribution code exists anywhere
in this repository prior to this prompt.

## 2. What was built

### `analytics/anomaly.py` (new)

`detect_anomalies(points, settings=None) -> AnomalyDetectionResult` —
scans a whole chronological `(period, value)` series against five
independent, individually-configurable methods, each implemented as its
own small, pure helper:

1. **Threshold** (`Settings.anomaly_absolute_threshold`, `None`/off by
   default — no safe generic unit to default to): `|value -
   previous_value| > threshold`.
2. **Percent change** (`Settings.anomaly_percent_change_threshold`,
   default `20.0`): point-over-point, the identical zero-denominator
   guard `agent.insight._compute_trend`/`analytics.engine
   ._compute_growth_stat` already established.
3. **Rolling z-score** (`Settings.anomaly_rolling_window`, default `5`;
   `Settings.anomaly_zscore_threshold`, default `2.0`): mean/population-
   stddev of exactly the prior `window` points (never the point itself)
   — a genuinely *rolling*, not global, baseline. **Found and disclosed
   during testing, not hidden**: a sustained, perfectly linear trend
   produces a *constant* rolling z-score once the window is full (a
   mathematical property of evenly-spaced data with a simple, non-
   detrended rolling mean, not a bug) — still materially better than a
   global mean, which would stay permanently skewed by any one-off spike
   long after it rolled out of a rolling window's own memory (both
   properties proven directly in `tests/test_analytics_anomaly.py`).
4. **IQR** (`Settings.anomaly_iqr_multiplier`, default `1.5`): one
   whole-series `[Q1 - k·IQR, Q3 + k·IQR]` fence (≥4 points, the
   identical minimum Prompt 13's own categorical IQR check already
   uses), a global, complementary signal to (3).
5. **Seasonal** (`Settings.anomaly_seasonal_period`, `None`/off by
   default): "where sufficient" — only evaluated when at least one
   same-lag prior occurrence exists; 2+ occurrences get a proper
   mean/stddev seasonal z-score (skipped, not divided-by-zero, if those
   occurrences happen to be identical), exactly 1 occurrence falls back
   to a plain, lower-confidence percent-change check against that one
   point, clearly distinguished in the signal's own `formula` text.

Every flagged point becomes one `AnomalyPoint` carrying every
`AnomalySignal` that fired for it (method, baseline value(s), deviation,
threshold, formula) — never collapsed into an undifferentiated boolean.
`AnomalyDetectionResult` also reports `methods_evaluated` (which methods
had enough history/configuration to run at all) and
`insufficient_data_reasons` (every method skipped, and why).

### `analytics/root_cause.py` (new)

`investigate_root_cause(current_rows, baseline_rows, columns,
dimension_columns, value_column, settings=None) -> RootCauseResult` —
the prompt's own named 7-step flow, each step a clearly separated piece
of one function:

1. **anomaly (magnitude)** / 2. **baseline**: `current_total`/
   `baseline_total` (sums, nulls excluded) → `magnitude = current_total
   - baseline_total`, `magnitude_percent` (zero-denominator guarded).
   Both totals are always returned, even when evidence later turns out
   insufficient — a caller can always show "here's what changed."
3. **dimension breakdown**: `analytics.engine.group_and_sum_by_label`
   (promoted from private `_totals_by_label` — same body, now public,
   reused verbatim rather than re-implemented — master-contract rule
   2/3) run once per period, over one or more dimension columns (a
   multidimensional breakdown gets the identical flattened composite-
   label treatment Prompt 13's own `MULTIDIMENSIONAL` shape uses).
4. **contribution**: over the *union* of labels seen in either period
   (a label present in only one period is a genuine new/dropped
   contributor, not an error — the missing side defaults to `0.0`),
   `contribution = current - baseline`, `contribution_percent = 100 *
   contribution / magnitude`.
5. **ranking**: sorted by `|contribution|` descending.
6. **validation**: only a contributor whose `|contribution_percent| >=
   Settings.root_cause_min_contribution_percent` (default `10.0`)
   survives — everything else is counted (not detailed) in
   `limitations` as excluded noise, never silently dropped without a
   trace.
7. **evidence**: `has_sufficient_evidence = bool(contributors)` — the
   literal structural enforcement of this prompt's acceptance criterion
   (see §3). `confidence` (only when evidence is sufficient) is
   `min(1.0, sum(|contribution_percent| of validated contributors) /
   100)` — the deterministic "how much of the total change do these
   contributors collectively explain" fraction.

Insufficient-evidence paths, each with its own disclosed `limitations`
reason: empty `current_rows`/`baseline_rows`; an unknown `value_column`/
`dimension_columns` entry; no `dimension_columns` given at all; a
`magnitude` of exactly `0` (nothing to explain); zero contributors
clearing the validation bar (a genuinely broadly-distributed change,
live-verified — see §5).

### `analytics/engine.py` (extended, minimal diff)

`_totals_by_label` renamed to public `group_and_sum_by_label` (zero
behavior change — re-run `tests/test_analytics_engine.py` unmodified
proves it). `_growth_finding` was split into a pure `_compute_growth_stat`
(returns the typed `GrowthStat` itself) and a thin `_growth_finding`
wrapper (builds the `AnalyticsFinding`/claim text around it) — this is
what lets `compute_analytics_result`'s `TIME_SERIES` branch read
`growth_stat.points` directly to feed the anomaly detector, without a
second, duplicate re-computation of the same series. The **only** other
change: immediately after computing growth, when
`Settings.enable_anomaly_detection` is `True`, calls `detect_anomalies`
on that same series and appends one `ANOMALY` finding per flagged point.
Every other shape/branch is byte-for-byte untouched.

### `analytics/models.py` (extended, additive only)

New `AnomalyMethod` enum (5 values); `AnomalySignal`/`AnomalyPoint`/
`AnomalyDetectionResult` typed models; new `AnalyticsFindingKind.ANOMALY`
+ `AnalyticsFinding.anomaly: AnomalyPoint | None` (one finding per
flagged point, mirroring the existing one-finding-per-outlier
precedent). New `ContributorStat`/`RootCauseResult` — deliberately
**not** wrapped in `AnalyticsFinding`/`AnalyticsResult` (a root-cause
investigation spans two datasets, not one query result's own findings;
forcing it through that single-dataset taxonomy would be a worse fit
than its own small, clearly-named model).

### Config (`config/settings.py`)

`enable_anomaly_detection: bool = True`, `anomaly_zscore_threshold:
float = 2.0`, `anomaly_iqr_multiplier: float = 1.5`,
`anomaly_percent_change_threshold: float = 20.0`,
`anomaly_absolute_threshold: float | None = None`,
`anomaly_rolling_window: int = Field(default=5, gt=1)`,
`anomaly_seasonal_period: int | None = None`,
`root_cause_min_contribution_percent: float = Field(default=10.0,
gt=0)` — same on-by-default-but-escapable, fully-documented posture as
every flag in this series.

**No changes to `agent/nodes.py`, `agent/graph.py`, `agent/state.py`,
`api/schemas.py`, `api/main.py`** — anomaly detection rides the
already-live `AgentState["analytical_result"]`/`AskResponse
.analytical_result` surface Prompt 13 built; root-cause has no live
call site in this pass (see §4), so nothing new to wire.

## 3. Proof: no root cause is stated without supporting evidence

- `investigate_root_cause` has exactly one return statement that sets
  `has_sufficient_evidence=True` — the final one, reached only after the
  validation loop (step 6) has already populated `validated` with at
  least one `ContributorStat`. Every earlier `return` (empty inputs,
  unknown column, no dimension columns, zero magnitude) explicitly sets
  `has_sufficient_evidence=False` with `contributors=()` and
  `confidence=None` left at their defaults.
- `tests/test_analytics_root_cause.py::TestInsufficientEvidence
  ::test_broadly_distributed_change_has_no_single_contributor` proves
  this isn't just a hand-picked edge case: 12 dimension values each
  moving by a similar small amount (no single one explains ≥10% of the
  total change) correctly yields `has_sufficient_evidence=False`,
  `contributors=()`, `confidence=None` — the structurally-enforced
  acceptance criterion, verified.
- **Live-verified against real data** (§5 below): a real year-over-year
  category/region breakdown correctly produced `has_sufficient_evidence
  =True` with exactly the contributors that clear the bar, and excluded
  six smaller ones as disclosed noise — the engine doesn't just handle
  hand-built fixtures correctly, it handles genuine, messy, real-world
  distributions correctly too.

## 4. Explicitly out of scope

- **Root-cause/contribution analysis is not wired into any live
  LangGraph node.** It inherently needs *two* datasets — this
  application's pipeline generates exactly one SQL query per question
  today, and there is no existing mechanism that decides "this question
  needs a second, baseline-comparison query" and issues one. Building
  that decision/generation mechanism is a materially larger, separate
  undertaking this inspection pass is not silently absorbing into what
  was scoped as a computation-engine prompt. `investigate_root_cause` is
  fully built and fully tested, ready for that future wiring.
- **Seasonal gap/missing-period detection does not exist for the
  anomaly detector** (unlike `analytics.engine`'s own `GrowthStat
  .missing_periods` for the *growth* series) — the seasonal method
  itself only ever compares *values* at a configured lag, it doesn't
  separately verify the lag points themselves form a complete,
  gap-free history.
- **No per-role restriction on who can call these functions** — neither
  module performs I/O or touches any identity/authorization context;
  both inherit whatever already gates the one live caller
  (`compute_analytics_node`, itself gated by `/ask`'s existing
  authorization).
- **Rolling z-score has no detrending step** (§2, method 3) — a
  disclosed, real limitation, not a silent gap.

## 5. Testing

4 test files touched (2 new, 2 extended), 52 new tests (full-suite
delta: 3006 → 3058):

- `tests/test_analytics_anomaly.py` (new, 26) — one class per method:
  a clear anomaly case, a clear no-anomaly case, and an insufficient-
  data case each; the discovered-and-disclosed linear-trend limitation
  (explicitly asserted, not hidden) alongside a test proving the rolling
  baseline still recovers faster than a global one would; the seasonal
  method's 2+-occurrence z-score, its single-occurrence percent-change
  fallback, and its zero-stddev-degenerate-history skip; a point flagged
  by multiple methods at once; formula/threshold/engine-version presence
  on every signal.
- `tests/test_analytics_root_cause.py` (new, 16) — the full 7-step flow
  on a single-dominant-contributor case (ranking, contribution-percent,
  confidence all verified against hand-computed values), a
  multidimensional composite-label case, a new/dropped-contributor case
  each, the broadly-distributed insufficient-evidence case, empty
  current/baseline rows, no dimension columns, zero magnitude, an
  unknown value/dimension column, and the validation threshold's own
  configurability (a higher bar excludes more, a lower bar admits more).
- `tests/test_analytics_engine.py` (extended, +3,
  `TestTimeSeriesAnomalyIntegration`) — a spiky `TIME_SERIES` result
  produces real `ANOMALY` findings with non-empty signals; a stable one
  produces none; `enable_anomaly_detection=False` reproduces the
  pre-Prompt-14 finding set exactly (the explicit fallback-preservation
  regression test) — re-run unmodified otherwise, proving the
  `_totals_by_label` → `group_and_sum_by_label` rename and the
  `_growth_finding` split are zero-behavior-change refactors.
- `tests/test_analytics_models.py` (extended, +7) — every new enum/
  model shape.
- `tests/test_analytics_provider.py`, `tests/test_insight.py` — re-run
  completely unmodified; both pass, proving the legacy adapter and the
  benchmark-pinned insight module were not touched by this prompt.

**Full suite: 3058 tests pass, zero regressions** (`pytest -q`, re-run
after every change in this prompt). `ruff check`/`black --check`/`mypy`/
`bandit` clean on every new/touched file, zero findings introduced.

**Live verification** against the real AdventureWorksDW2025 connection:
a real yearly `Quantity` series (`[14, 2216, 3397, 52801, 1970]`) was
fed through `detect_anomalies` — correctly flagged the real 2013 spike
by both `percent_change` and `iqr`, with `rolling_zscore` correctly
recorded as insufficient (fewer than 6 points available). A real
category/region breakdown for two consecutive real years was fed
through `investigate_root_cause` — correctly computed a magnitude of
`-50,831` (`-96.3%`), correctly identified `Accessories / North America`
(32.5%), `Accessories / Europe` (19.4%), and `Accessories / Pacific`
(12.7%) as validated contributors (confidence `0.65`), and correctly
excluded six smaller dimension values as noise below the 10% bar.

## 6. Security / tenant-isolation / performance review

- **Security**: both modules are pure functions with no I/O and no LLM
  call of their own — neither can be prompt-injected, and both operate
  only on data already fetched by the pre-existing, unmodified SQL
  pipeline. Every finding/result is tagged (or, for
  `RootCauseResult`/`AnomalyDetectionResult`, implicitly carries)
  `DataTruthLevel.DATABASE_FACT`-equivalent provenance — pure arithmetic,
  never an LLM-authored claim, the literal mechanism behind "no root
  cause is stated without supporting evidence."
- **Tenant isolation**: no new tenant-scoped data read or written.
  `compute_analytics_node`'s own existing scope (already-fetched
  `result_columns`/`result_rows`) is unchanged; `investigate_root_cause`
  isn't called from anywhere live in this pass, so it introduces no new
  live data-access surface at all.
- **Performance**: anomaly detection adds one more pure-Python pass over
  a series already held in memory (the same series `GrowthStat` already
  computed) — no new network call, no new database round-trip.
  Disabling `Settings.enable_anomaly_detection` removes this cost
  entirely with zero other behavior change. `investigate_root_cause` has
  no live performance impact at all (not called from any live path).

## 7. Known limitations / remaining risks

- Root-cause/contribution analysis has no live call site — see §4.
- Rolling z-score has no detrending step — a sustained linear trend can
  still be flagged — see §2/§4.
- The seasonal method doesn't itself verify its lag history is
  gap-free — see §4.
- `investigate_root_cause`'s multidimensional breakdown is the same
  flattened-composite-label approach Prompt 13's own `MULTIDIMENSIONAL`
  shape uses, not true N-way pivot/cube decomposition (e.g. it cannot
  separate "how much of this contributor's change was really about the
  category vs. the region" within one composite label).

## Recommended next prompt

Teach the agent pipeline to recognize when a question is asking "why"
something changed (a natural extension of Prompt 11's
`AnalyticalIntentType.ROOT_CAUSE`/`ANOMALY` categories, which already
exist in the intent classifier but have no computation behind them
today) and, when it does, generate the *second*, baseline-comparison
query `investigate_root_cause` needs — the concrete next step that
closes this prompt's one disclosed "not live-wired" gap. Alternatively,
wire `analytics.engine.compute_analytics_result`'s now-even-richer
output (including `ANOMALY` findings) into the live insight-narrative
prompt and re-run the live-LLM benchmark — the carried-forward
recommendation Prompts 04-13 have each named for this same deferred
decision.
