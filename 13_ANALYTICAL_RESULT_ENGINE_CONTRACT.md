# 13 — Deterministic Analytical Result Engine

Prompt 13 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`12_ANALYTICAL_PLANNING_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/TEST/
REVIEW/FIX/DOCUMENT** — one new computation module reusing the
pre-existing typed-findings contract, one new LangGraph node sitting
between `execute_sql` and `generate_insight`, and additive `AskResponse`/
`config.settings` surface. No existing route, node, prompt, or
`Settings` field changed behavior for a question with
`enable_analytics_engine` off, and `agent/insight.py` — the live,
benchmark-pinned insight-narrative module — was not touched at all.

## 1. Inspection: what already existed vs. the real gap

`agent/insight.py`'s `summarize_result()` already computes a narrow
statistic set over a query result (min/max/sum per numeric column,
population stddev/coefficient-of-variation, a single first-vs-last
`TrendStat`, z-score outliers on ≥3 categories), and a fully built,
fully tested `analytics/` package (`AnalyticsProvider` protocol,
`AnalyticsFinding`/`AnalyticsResult`) already exists as the typed home
for "computed facts" — but `analytics/provider.py`'s
`ResultSummaryAnalyticsProvider` is a zero-new-computation adapter over
that same narrow set (proven by its own
`test_zero_new_computation_findings_are_a_pure_subset_of_summary_data`
test), and it is called from nowhere live (confirmed by an exhaustive
repo-wide grep: only `analytics/`'s own files and its two test files
reference it). Median, true percentiles, a full multi-period growth
series, a complete ranking, raw-table column stats, and a
"formula/calculation version" concept did not exist anywhere in the
codebase — confirmed by exhaustive grep across `agent/`, `analytics/`,
`db/`, `eval/`, `retrieval/`, `semantic/` for `percentile|median|
stddev|variance|growth|change_percent|outlier|distribution|z_score|IQR`;
every other hit was either `agent/insight.py` itself, a SQL-dialect-
capability check (`percentile_cont` as a database feature flag, not a
Python calculation), or an unrelated use of the same English word
(database-engine behavioral "variance," eval-harness latency "p95,"
rate-limiter key "growth").

`02_TARGET_ARCHITECTURE.md` §5's own "stubbed today, wired later" table
scoped `analytics/`'s own "done" bar narrowly: call `.analyze()` from a
node and surface `AnalyticsResult` in `AskResponse` — it never
anticipated the full stat set this prompt's objective names. That
narrower bar is satisfied as a side effect of this prompt (via the API
surface addition below), but the actual deliverable is the real
calculator, not just the wiring.

## 2. What was built

### The engine — `analytics/engine.py` (new)

`compute_analytics_result(columns, rows, settings=None) -> AnalyticsResult`
— pure, deterministic, no LLM, no I/O, same posture `agent/insight.py`
already establishes. **Deliberately independent of `agent.insight
.summarize_result`, not a reuse of its code**: that function's exact
output is pinned to this project's live-LLM benchmark numbers (see
CLAUDE.md's "AI Data Analyst depth" section) and must stay
byte-identical — verified by re-running `tests/test_insight.py`
completely unmodified. Shared *conventions* (population stddev via
`statistics.pstdev`, the z-score outlier threshold of `2.0`) are kept
consistent with `agent.insight` by matching constant, not by import.

1. **Shape classification** (`classify_result_shape` — `ResultShape`:
   `SCALAR`/`TIME_SERIES`/`CATEGORICAL_AGGREGATE`/`MULTIDIMENSIONAL`/
   `RAW_TABLE`/`EMPTY`): 0 rows → `EMPTY`; 1 row → `SCALAR`; else, finds
   a usable (label, value) column pair the same way `agent.insight`
   already does (last numeric column as value, first non-numeric column
   as label, with the identical numeric-vs-numeric fallback) — no usable
   pair → `RAW_TABLE`; 2+ label columns → `MULTIDIMENSIONAL`; a single
   label column matching a recognized period pattern (4-digit year,
   `YYYY-MM`, ISO date) for a majority of rows → `TIME_SERIES`;
   otherwise → `CATEGORICAL_AGGREGATE`.
2. **Column-level stats** (every shape, every column): count, null
   count, distinct count (**including numeric columns** — a real gap
   `agent.insight.ColumnStat.distinct_count` has today, only ever set
   for non-numeric columns), min/max/mean/median, variance/stddev/
   coefficient-of-variation, and percentiles (p25/p50/p75/p90/p95/p99
   via `statistics.quantiles(..., method="inclusive")`, stdlib, no new
   dependency). **Null handling**: a column's numeric-ness is judged
   from its *non-null* values (nulls excluded from aggregates, counted
   via `null_count`) — deliberately different from `agent.insight`'s
   stricter "every row including nulls must be numeric" rule, confined
   to this new, independent engine by design.
3. **Growth** (`TIME_SERIES` only): the full period-by-period series
   (not just first-vs-last), each step's `change_percent` guarded
   against a zero-denominator (`None`, never a crash or a misleading
   "infinite growth" claim — the identical guard `agent.insight
   ._compute_trend` already established, applied per-step across the
   whole series instead of once). **Missing-period detection**: only
   attempted for `year`/`year_month` label shapes (an ISO date is
   recognized for *classification* but deliberately gets no gap
   detection — its real reporting cadence isn't inferable from the
   label shape alone, and guessing wrong would be worse than reporting
   none).
4. **Ranking** (`CATEGORICAL_AGGREGATE`/`MULTIDIMENSIONAL`): every
   distinct label (a flattened composite for multidimensional) ranked
   by value descending, each entry carrying `share_percent` — this
   single ranked list **is** the "distribution" requirement too (a
   distribution is the same breakdown viewed by its share-of-total
   percentages, not a second computation). Capped at
   `Settings.analytics_ranking_max_entries` (default 50), flagging
   `truncated=True` when the real count exceeds it.
5. **Outliers** (`CATEGORICAL_AGGREGATE`/`MULTIDIMENSIONAL`, over the
   same per-label totals the ranking is built from): both z-score
   (`agent.insight`'s own 2.0-threshold/≥3-category convention) **and**
   IQR (`[Q1 - 1.5·IQR, Q3 + 1.5·IQR]`, ≥4 categories) — two
   complementary, independent methods; a live-data test in this pass
   confirmed they can genuinely disagree (a single extreme outlier
   among few points can self-inflate the population stddev enough to
   mask its own z-score — a real, known z-score limitation, not an
   engine bug — while IQR still flags it correctly).
6. **Insufficient data / edge cases**: every skipped calculation
   (fewer than 2 points for growth, fewer than 3/4 categories for
   z-score/IQR outliers respectively, fewer than 2 non-null values for
   median/percentile/variance, an empty result, a zero stddev/IQR) is
   recorded in `AnalyticsResult.insufficient_data_reasons` — explicit,
   inspectable "why this wasn't computed," never a silent omission.
7. **Formula/version metadata**: `ANALYTICS_ENGINE_VERSION = "1.0.0"`
   (lives in `analytics/models.py`, imported by the engine — not the
   reverse, avoiding a circular import) stamped onto every
   `AnalyticsResult.engine_version`; every derived finding
   (median/percentile/variance/growth/ranking/outlier) carries its own
   `formula: str` — the literal "record formula/version metadata"
   requirement (rule 11: versioning).
8. **Provenance**: every `AnalyticsFinding.claim.level` is always
   `DataTruthLevel.DATABASE_FACT` (`agent.provenance`) — never
   promoted, never an LLM-authored claim (master rules 9/10). This is
   the literal mechanism behind the acceptance criterion "LLMs explain
   computed facts but do not become the authoritative calculator": the
   engine is the sole author of every number here, by construction —
   nothing in this module ever calls an LLM, and nothing downstream of
   it is permitted to recompute a number it already produced.

### Typed contracts — `analytics/models.py` (extended, additive only)

New `ResultShape` enum; five new `AnalyticsFindingKind` values (`MEDIAN`/
`PERCENTILE`/`DISTINCT_COUNT`/`GROWTH`/`RANKING`, plus `COLUMN_SUMMARY`
for the basic count/null/min/max/mean bundle not covered by any other
kind) and their typed sub-stats (`MedianStat`/`PercentileStat`/
`DistinctCountStat`/`GrowthPoint`+`GrowthStat`/`RankingEntry`+
`RankingStat`/`ColumnSummaryStat`, each with its own `formula: str`
where applicable); a new, typed `VarianceStat` (the new engine's own
counterpart to the legacy `VARIANCE` kind's text-only `variance_column:
str` field) and a new `OutlierFinding` (distinct from the legacy
`OutlierStat`-backed `outlier` field — this one tags which of the two
methods, z-score or IQR, flagged it). `AnalyticsFinding` gained one new
optional field per addition above; `AnalyticsResult` gained
`shape`/`engine_version`/`insufficient_data_reasons`, all defaulted so
every pre-existing construction call — including
`ResultSummaryAnalyticsProvider`'s own — keeps validating unchanged
(proven by re-running `tests/test_analytics_provider.py` and the
pre-existing part of `tests/test_analytics_models.py` completely
unmodified).

### Node + graph wiring

New `agent.nodes.compute_analytics_node`, wired
`execute_sql --(succeeded)--> compute_analytics --> generate_insight`
(two straight edges — `route_after_execution`'s own routing logic is
unchanged; only its `"succeeded"` target in `agent/graph.py`'s edge
table moved from `generate_insight` to `compute_analytics`). Gated by
new `Settings.enable_analytics_engine` (default `True`) — runs on
**every** successful execution, unlike `generate_insight_node` right
after it (no `enable_insight`-style skip: a pure, zero-I/O computation
has no narrative cost or risk to gate on). Delegates entirely to
`analytics.engine.compute_analytics_result`; stores the result as a
plain dict on `AgentState["analytical_result"]`. Fails open on any
unexpected error — logged, `state["analytical_result"]` stays `None`,
never a reason the question itself fails, the same posture every other
accuracy-aid node in this graph already takes.

### API surface (additive, backward compatible)

`api/schemas.py::AskResponse` gained `analytical_result: dict[str, Any]
| None = None`, populated in `api/main.py`'s `_ask_response_from_state`
from `state.get("analytical_result")` — a direct passthrough, not
text-redacted, matching `result_rows`/`result_columns`'s own existing
treatment (a ranking/outlier label is the same underlying row data
already present in `result_rows`, just re-aggregated; redaction in this
codebase targets narrative/LLM-generated text, not raw data values).
Closes `02_TARGET_ARCHITECTURE.md` §5's own narrower "wired" bar for
`analytics/` without touching the live LLM prompt.

## 3. Proof: the live, benchmark-pinned insight pipeline is untouched

- `agent/insight.py` has **zero diff** in this prompt — `git diff` on
  this file is empty. `tests/test_insight.py`'s full 55-test suite
  passes unmodified, the direct proof that `summarize_result()`/
  `should_skip_insight()`/`is_insight_grounded()` behave byte-identically
  to their pre-Prompt-13 shape.
- `agent.llm_client._INSIGHT_SYSTEM_PROMPT`/`_build_insight_prompt` are
  untouched — `generate_insight_node` calls `generate_analytics_result`
  for nothing; it still only ever calls `summarize_result` and
  `generate_insight_from_llm`, exactly as before.
- `analytics/provider.py` has zero diff; `tests/test_analytics_provider.py`
  passes unmodified.
- **Never short-circuits the graph**: `compute_analytics` has exactly
  one outgoing edge (`conditional=False`) to `generate_insight` —
  verified directly
  (`tests/test_sql_agent_integration.py::test_compute_analytics_sits_between_execute_sql_and_generate_insight`).
- **Fails open, never blocks**: `test_fails_open_on_unexpected_computation_error`
  (`tests/test_agent_nodes.py::TestComputeAnalyticsNode`) proves an
  unexpected exception from the engine leaves `generate_insight_node`
  completely free to run next, exactly as it would have pre-Prompt-13.

## 4. Explicitly out of scope

- **The live insight narrative prompt is not extended to use this
  engine's output** — see §1/§3 above for the full, disclosed reasoning
  (the benchmark-pinned prompt text). Recommended as the natural next
  step, once a live-LLM benchmark re-run is budgeted.
- **Missing-period detection recognizes only `year`/`year_month` label
  shapes** — a full ISO date is classification-recognized but
  deliberately gets no gap detection (ambiguous real cadence); no
  general natural-language date parsing (`"Q1 2021"`, `"Jan 2021"`) is
  attempted at all, a disclosed, bounded scope avoiding a fragile,
  heavyweight date-parsing dependency.
- **Multidimensional handling is a flattened composite-key ranking**,
  not true N-way pivot/cube analysis — a 3+-dimension result still gets
  one ranked list keyed by every dimension's value joined together, not
  a per-dimension breakdown.
- **No per-role restriction on who can see `AskResponse
  .analytical_result`** — it inherits whatever authorization already
  gates `/ask` itself; no new, finer-grained permission was introduced
  (nothing in this field is more sensitive than `result_rows`, which
  carries the identical underlying data).

## 5. Testing

6 test files touched (4 new, 2 extended), 68 new tests (full-suite delta:
2938 → 3006):

- `tests/test_analytics_engine.py` (new, 47) — shape classification (all
  six shapes, including the numeric-vs-numeric fallback), period-column
  classification, every shape's own finding set (scalar/empty/
  categorical/time-series/multidimensional/raw-table), null handling
  (numeric-with-nulls, all-null column, nulls excluded from ranking),
  percentile/median correctness against a hand-verified 1-10 dataset,
  variance edge cases (identical values, zero-mean CoV), both outlier
  methods (including the documented zscore/IQR disagreement case),
  insufficient-data-reasons population for every skip category, and
  formula/engine-version presence on every derived finding type.
- `tests/test_analytics_models.py` (extended, +19) — every new enum
  value/typed-stat model, a dedicated regression class proving the
  pre-existing `AnalyticsResult`/`AnalyticsFinding` construction shape
  and every new field's `None` default are unaffected.
- `tests/test_analytics_provider.py` — re-run unmodified; passes.
- `tests/test_insight.py` — re-run unmodified; passes (55 tests).
- `tests/test_agent_nodes.py` (+6, `TestComputeAnalyticsNode`) —
  disabled-flag skip, a real result stored on success, an empty result
  still produces a result (not a crash), fail-open on an unexpected
  engine error, a defensive missing-state-fields case, and a direct
  function-level proof that chaining into `generate_insight_node`
  afterward still works.
- `tests/test_sql_agent_integration.py` (+1 new, +1 extended) — the new
  `compute_analytics`-wiring structural proof, plus the pre-existing
  "all nodes present" test extended to include it.
- `tests/test_api_schemas.py` (new, 3) — `AskResponse.analytical_result`
  defaults to `None`, round-trips a plain dict through `model_dump`, and
  a pre-Prompt-13-shaped construction call (no such field passed) is
  unaffected.

**Full suite: 3006 tests pass, zero regressions** (`pytest -q`, re-run
after every change in this prompt). `ruff check`/`black --check`/`mypy`/
`bandit` clean on every new/touched file (zero findings introduced by
this prompt; one pre-existing, unrelated `api/main.py` finding — far
from anything this prompt touched — was confirmed via `git diff` to
predate this work and was left alone).

**Live verification** (same posture as Prompt 12's own): real driver
output from a live SQL Server LocalDB connection (`AdventureWorksDW2025`,
500 real rows across `EnglishProductCategoryName`/`Region`/`Quantity`)
was fed directly through `compute_analytics_result` — correctly
classified `MULTIDIMENSIONAL`, produced a real ranking (`"Bikes /
Pacific"` at 40.8% share), and correctly recorded an
`insufficient_data_reasons` entry for IQR outliers (only 3 distinct
category/region combinations, below the 4-category minimum). A full,
real `agent.graph.run_agent()` call (real Ollama, real DB) for a simple
scalar question completed successfully end-to-end with
`compute_analytics` correctly populating a `SCALAR`-shaped
`analytical_result` alongside the existing, unaffected successful
execution path.

## 6. Security / tenant-isolation / performance review

- **Security**: `analytics/engine.py` is a pure function with no I/O and
  no LLM call of its own — it cannot itself be prompt-injected, and it
  reads only data already fetched and already validated by the
  pre-existing, unmodified SQL-safety pipeline (`agent.sql_validator`,
  `execute_sql_node`). Every finding is `DATABASE_FACT`-tagged by
  construction (never set to anything else anywhere in this module),
  which is the structural enforcement of "the LLM never becomes the
  calculator" — there is no code path in this prompt by which an LLM
  output could reach `AnalyticsFinding.claim.value`.
- **Tenant isolation**: no new tenant-scoped data is read or written —
  `compute_analytics_node` reads only `result_columns`/`result_rows`,
  already resolved and already scoped by `execute_sql_node` earlier in
  the same run. No new database query of any kind.
- **Performance**: one additional pure-Python computation pass per
  successful execution, over data already held in memory (the same
  result set `generate_insight_node` already summarizes) — no new
  network call, no new database round-trip. Disabling
  `Settings.enable_analytics_engine` removes this cost entirely with
  zero other behavior change.

## 7. Known limitations / remaining risks

- The live insight-narrative LLM prompt doesn't yet surface any of this
  engine's richer findings — see §4.
- Missing-period detection and multidimensional ranking are both
  deliberately bounded, disclosed scopes — see §4.
- No UI currently renders `AskResponse.analytical_result` — it is
  computed, tested, and reachable via the API today, not yet visible in
  the React dashboard.
- Percentile/median/variance are computed per-column independently;
  there is no cross-column statistic (e.g. correlation between two
  numeric columns) in this pass.

## Recommended next prompt

Wire `analytics.engine.compute_analytics_result`'s output into the live
insight-narrative prompt (`agent.llm_client._build_insight_prompt`) and
re-run `docs/EVALUATION_CURRENT.md`'s live-LLM benchmark to confirm
correctness is preserved — the same carried-forward recommendation
Prompts 04-12 have each named for the *original* trend/outlier/stddev
fields, now extended to this prompt's much richer set. Alternatively, a
React dashboard surface for `AskResponse.analytical_result` (a trend/
ranking/outlier badge on the results table), per
`docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md`'s own existing P1 roadmap
item.
