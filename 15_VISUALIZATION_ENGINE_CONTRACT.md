# 15 — Visualization & Analytical Presentation Engine

Prompt 15 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`14_ANOMALY_ROOT_CAUSE_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/TEST/
REVIEW/FIX/DOCUMENT** — one new backend module, a minimal-diff extension
of the already-live `frontend/src/lib/chartEngine.ts`, additive
`api/schemas.py`/`api/main.py` wiring, and additive `config.settings`
surface. No existing chart-picker behavior, API contract field, or route
changed shape; every new field is optional/defaulted.

## 1. Inspection: what already existed vs. the real gap

`frontend/src/lib/chartEngine.ts` already had a mature, fully client-side
charting system: 11 Chart.js-backed types, a real per-type validity
engine (`getChartTypeOptions`), sort/top-N/date-grouping transforms
(`prepareChart`), and a validated accessible color palette.
`agent/result_charting.py`'s `classify_columns`/`recommend_chart` was a
deliberately thin backend "seed" — numeric/date/text classification plus
one suggested starting type — wired only into `POST /execute`'s
`ExecuteResponse.column_types`/`chart_recommendation`. CLAUDE.md states
as an explicit "invariant that must not regress" that the frontend, not
an LLM or a backend heuristic alone, has final authority over what's
actually renderable — this replaced an earlier always-on,
backend-rendered Plotly design specifically for that reason.

Three result shapes had **no** mapping at all before this prompt:
single-numeric-column distributions, box-and-whisker summaries, and
geography. Grep confirmed no existing module in this codebase inferred
aggregation/format/title text from a column's own name, built sort/limit/
accessibility metadata, or detected a geography-shaped column anywhere.

**The one real tension, resolved honestly**: "distributions to
histogram/box" and "geography to map" ask for three chart types this
app's chosen stack (Chart.js, no plugins) cannot all render today — a
true box-and-whisker plot and a real map both need a library this app
doesn't have, and "do not introduce duplicate chart libraries" says not
to add one casually. Resolution: **histogram is genuinely implemented**
(a histogram is just a bar chart of binned counts — zero new
dependencies). **Box and map are real, detected concepts in the new
backend taxonomy**, but gated behind two new, off-by-default `Settings`
flags (`enable_box_plot_charts`/`enable_map_charts`) — the deterministic
engine never actually *recommends* either today, since doing so would
violate this prompt's own acceptance criterion ("frontend can render
stable chart specifications"). A distribution falls back to histogram and
a geography-shaped breakdown falls back to bar, both genuinely renderable
right now.

## 2. What was built

### `analytics/visualization.py` (new)

`build_chart_spec(columns, rows, column_types, analytics_result=None,
settings=None) -> ChartSpec | None` — deterministic, no LLM, no I/O, the
same posture every other `analytics/` module already establishes.

1. **Shape → chart type mapping**: 0 rows → `None`; 1 row with ≥1 numeric
   column → `KPI`; a date column + ≥1 numeric → `LINE`; a text column +
   ≥1 numeric → `BAR` (or `MAP` if that text column is geography-shaped
   **and** `enable_map_charts`); exactly 2 numeric columns → `SCATTER`
   (or `MAP` if both are a detected latitude/longitude pair **and**
   `enable_map_charts`); exactly 1 numeric column with ≥5 rows →
   `HISTOGRAM` (or `BOX` if `enable_box_plot_charts`); anything else
   (all-text, no numeric, a raw 3+-numeric-column table) → `None` — table
   is always the honest fallback.
2. **Geography detection** (`detect_geography_columns`): a small,
   explicitly disclosed, non-exhaustive name-pattern list (country/state/
   province/region/city/territory; latitude/lat; longitude/lon/lng).
   Runs **regardless** of whether map rendering is enabled — the
   classification itself is always real and always computed; only the
   chart-type *recommendation* is gated.
3. **camelCase-aware tokenization** (`_name_tokens`/`_TOKEN_RE`), shared
   by geography detection, aggregation inference, format inference, and
   title generation — a real necessity, not an academic one: this
   project's own schemas use camelCase column names (e.g.
   `SalesTerritoryRegion`), where a plain `\b`-word-boundary regex would
   never match "region" at all (no boundary exists mid-camelCase).
4. **Fields**: one `ChartField` per column actually used, tagged with its
   `FieldRole` (x/y/series/group/geo_region/geo_latitude/geo_longitude),
   a best-effort `aggregation` inferred from the column's own name
   (`"Total..."`/`"Sum..."` → `"sum"`, `"Avg..."`/`"Average..."` →
   `"avg"`, `"Count..."`/`"Qty..."` → `"count"`, etc.) — descriptive
   metadata only, never a re-aggregation this module performs itself —
   and an inferred `format` (`"currency"`/`"percent"`/`"plain"`, the
   same three string values `frontend/src/lib/chartEngine.ts`'s own
   `NumberFormat` already uses).
5. **Title**: deterministically generated from field roles + inferred
   aggregation + chart type (e.g. `"Total Sales by Category"`,
   `"Distribution of Score"`, `"Revenue vs Quantity"`). A real bug was
   found and fixed during direct smoke-testing before any test was
   written: a column already stating its own aggregation (e.g.
   `"TotalRevenue"` → humanized `"Total Revenue"`) was getting
   double-prefixed (`"Total Total Revenue"`). Fixed by
   `_label_with_aggregation_prefix`, which checks — via the same shared
   tokenizer, so the check is exact, not a fragile substring match —
   whether the aggregation word is already one of the column's own name
   tokens before prefixing.
6. **Sort**: `x-asc` for a line/time-series, `y-desc` for a bar/map
   ranking shape — reusing the exact same `SortOrder` string vocabulary
   `chartEngine.ts` already defines.
7. **Limit / deterministic downsampling**: capped at
   `Settings.visualization_default_top_n` (new, default `20` — matches
   `chartEngine.ts`'s own existing hardcoded top-N default exactly, so
   the two stay consistent even though the frontend remains the final
   authority), with `is_downsampled`/a disclosed notice
   (`"Showing top 20 of 50 rows."`) when the cap actually applies — never
   silent.
8. **Accessibility metadata**: `alt_text`/`summary`, always non-empty.
   When an already-computed `analytics.engine.AnalyticsResult` is passed
   in (e.g. a fresh `compute_analytics_result` call at the same call
   site), reuses its `RANKING`/`GROWTH`/`COLUMN_SUMMARY` finding's own
   grounded claim text directly — verified live (see §5) against a real
   ranking result: the exact same claim text `analytics.engine` produced
   ("Pending ranks first on StepCount at 76 (59.4% of the total).")
   appears verbatim inside the chart's own `accessibility.alt_text`.
   Falls back to a generic, deterministic description (chart type +
   title + row count) otherwise.
9. **Version metadata**: every `ChartSpec` stamped with
   `VISUALIZATION_ENGINE_VERSION` — this series' standing "record
   formula/version metadata" convention (master-contract rule 11).

Typed models (`ChartType`, `FieldRole`, `ChartField`, `ChartSort`,
`AccessibilityMetadata`, `ChartSpec`) live in the same module — small and
self-contained enough not to warrant `analytics/`'s own separate
`models.py` split, unlike `analytics/engine.py`'s contracts, which have a
second real consumer (`analytics/anomaly.py`/`root_cause.py`).

### `frontend/src/lib/chartEngine.ts` (extended, minimal diff)

- `'histogram'` added to `ChartTypeId`/`ALL_CHART_TYPES`/
  `CHART_TYPE_LABELS`.
- `getChartTypeOptions`: a new validity rule — enabled only when exactly
  one numeric column exists **and no text/date column is also present**
  (matching `analytics.visualization.build_chart_spec`'s own identical
  rule — that module's single-numeric branch is only reached once its
  date/text branches have already been ruled out) and `rowCount >= 5`.
  **A real bug was found and fixed during this pass, not by a design
  review but by the extended test suite itself**: the first
  implementation only checked `numericCols.length !== 1`, never actually
  checking for a co-present text/date column — so a category+numeric
  result (e.g. `region, revenue`) would have been offered as a valid
  histogram candidate too, contradicting the rule's own disclosed reason
  text ("no category or date column"). Fixed by also requiring
  `textCols.length === 0 && dateCols.length === 0`.
- `prepareChart`: a new `histogram` branch — bins the single numeric
  column's values via `computeHistogramBins` (Sturges' rule,
  `ceil(log2(n) + 1)`, clamped to `[5, 20]` buckets, the maximum value
  clamped into the last bucket rather than spilling into a phantom
  bucket past the end) and emits `chartJsType: 'bar'` — the **exact same**
  rendering path `bar`/`bar-horizontal`/`bar-stacked` already use, so
  `ResultChart.tsx` needed zero changes (confirmed: it switches only on
  `chartJsType`, never on `ChartTypeId`).
- `defaultSeriesForType`: a histogram's series is just its one numeric
  column as `yColumns`, no x-axis column (binning computes its own
  x-axis from the data).
- `recommendChart`: a new, explicit histogram branch (after the existing
  scatter check) so a single-numeric-column result gets a clear,
  dedicated recommendation reason rather than falling through to the
  picker's generic "first enabled non-table type" fallback.
- `ChartPicker.tsx`: a second real bug, also only surfaced by actually
  running the test suite (not caught by `tsc --noEmit -p .` on its own —
  see §5's own note on why): `ICONS`, a separate
  `Record<ChartTypeId, LucideIcon>` the picker UI uses, had no
  `histogram` entry, which **crashed the component at render time**
  (`Element type is invalid: ... got: undefined`) the moment a histogram
  option entered the grid. Fixed by mapping `histogram` to the same
  `BarChart2` icon every other bar-shaped type already uses.
- **`'box'`/`'map'` are deliberately NOT added to the frontend's own
  `ChartTypeId`** in this pass — unlike histogram, there is no
  achievable-without-a-new-library rendering path for either today, and
  a permanently-disabled, never-enableable frontend type would be
  exactly the "half-finished implementation" this project's own
  standards warn against. The backend's `ChartType.BOX`/`.MAP` values and
  their gating flags exist specifically so a **future** prompt that adds
  a real rendering capability has a clean, already-tested backend
  classification to wire into — not dead frontend code today.

### API surface (additive, backward compatible)

- `api/schemas.py`: new `ChartFieldOut`/`ChartSortOut`/
  `AccessibilityMetadataOut`/`VisualizationSpecOut`, mirroring the
  backend models exactly — the same pattern `ChartRecommendationOut`
  already establishes for the pre-existing, untouched recommendation.
- `ExecuteResponse.visualization_spec: VisualizationSpecOut | None =
  None` (additive, defaulted — every pre-Prompt-15 construction call is
  unaffected, verified directly in `tests/test_api_schemas.py`).
- `api/main.py`'s `/execute` handler: immediately after computing
  `column_types` (the one existing call site for
  `classify_columns`/`recommend_chart`), now also calls
  `analytics.engine.compute_analytics_result` (Prompt 13, cheap/
  deterministic/already-tested) and `analytics.visualization
  .build_chart_spec`, adding `visualization_spec=...` to the existing
  `ExecuteResponse(...)` construction. Wrapped in the exact same
  try/except-log-and-continue shape `agent.nodes.compute_analytics_node`
  already uses — an accuracy aid must never block a successful "Confirm
  and Run," so a computation failure here degrades to
  `visualization_spec=None`, never a failed response.
- **`POST /ask`/`AskResponse` are untouched** — charting has only ever
  been an `/execute`-scoped concern in this codebase (confirmed:
  `AskResponse` has no `column_types`/`chart_recommendation` field
  either), and this prompt doesn't change that scope.
- **The new `visualization_spec` is deliberately not wired into
  `chartEngine.ts`'s own seed-selection (`recommendChart`'s
  `backendHint`) in this pass** — `chart_recommendation`/`column_types`
  keep powering today's chart picker completely unchanged;
  `visualization_spec` is parallel, additive API surface a future prompt
  can have the frontend start consuming, the same "compute + test +
  expose via API, defer the deepest UI wiring" posture Prompts 13/14 both
  already used successfully.

### Config (`config/settings.py`)

`visualization_default_top_n: int = Field(default=20, gt=0)`,
`enable_box_plot_charts: bool = False`, `enable_map_charts: bool =
False` — fully documented, off-by-default-with-a-stated-reason (no real
box-plot/map renderer exists in this build yet).

## 3. Explicitly out of scope

- A real box-and-whisker/map rendering capability — the backend
  classification/gating exists; no new charting library was added.
- Wiring `visualization_spec` into `chartEngine.ts`'s own seed-selection
  — `chart_recommendation` stays the frontend's actual seed in this pass.
- Per-source query decomposition, multi-source visualization synthesis,
  or anything touching `agent/orchestrator/` — this prompt is scoped
  entirely to the single-source `/execute` "Confirm and Run" path.
- Re-inferring numeric/date/text column types independently of
  `agent.result_charting.classify_columns` — `build_chart_spec` takes
  `column_types` as an input specifically so there is exactly one
  classification implementation in this codebase, not two drifting
  copies (see §5's live-testing note on that function's own disclosed
  `Decimal`-typed-column limitation, unrelated to and unmodified by this
  prompt).

## 4. Testing

- **`tests/test_visualization.py`** (new, 27 cases): empty/no-columns/
  no-numeric/raw-3-numeric-table/too-few-rows-for-histogram all → `None`;
  KPI/line/bar/scatter/histogram shape mappings; histogram→box gating;
  geography detection (region/lat/lon tagging, an unmatched name left
  untagged); geo-region→bar fallback with map charts off vs. →map with
  them on; lat/lon pair →scatter fallback vs. →map; large-result
  downsampling with the exact disclosed notice text; currency/percent
  format inference; count-aggregation title has no `"Count Count"`
  collision; the double-prefix title bug's own regression case
  (`"Total Total"` must never appear); accessibility falls back to a
  generic description without an `AnalyticsResult`, reuses a real one's
  ranking claim verbatim when given one, and is never empty either way;
  every spec stamped with the current engine version.
- **`frontend/src/lib/chartEngine.test.ts`** (extended, 18 new cases):
  histogram enabled/disabled per row count and per co-present category
  column (the validity-rule bug's own regression case);
  `computeHistogramBins`'s empty-input, identical-values-collapse-to-
  one-bucket, known-range-bucket-count-and-full-accounting, large-dataset
  bucket-count-clamped-to-`[5,20]`, and range-label behaviors;
  `recommendChart` returning a histogram recommendation; `prepareChart`
  reaching `chartJsType: 'bar'` for a histogram with no new rendering
  code.
- **`tests/test_api_schemas.py`** (extended, 3 new cases):
  `ExecuteResponse.visualization_spec` defaults to `None`, round-trips a
  constructed `VisualizationSpecOut` through `model_dump(mode="json")`,
  and every pre-Prompt-15 `ExecuteResponse` construction call (which
  never passed this field) is unaffected.

**Full suite results**: `pytest -q` → **3088 passed** (3058 before this
prompt + 27 + 3 new), zero regressions. Frontend Vitest → **287 passed**
across 41 files (45 in `chartEngine.test.ts` itself), zero regressions
once both bugs above were fixed. `ruff check`/`black --check`/`mypy`/
`bandit` clean on every new/touched Python file (one pre-existing,
unrelated `UP038` finding in `api/main.py`, already disclosed in
CLAUDE.md's "Known, pre-existing CI-hygiene gaps" note, untouched by this
prompt).

**A real process finding, not just a code one**: `npx tsc --noEmit -p .`
(the command this session initially ran) silently checks **zero files** —
this repo's root `tsconfig.json` is a solution file (`"files": []` plus
`references`), so a bare `-p .` with no `-b`/build flag finds nothing to
type-check and exits 0 regardless of real errors. Both frontend bugs
above (`ChartPicker.tsx`'s missing `ICONS.histogram` entry, a test file's
own `reduce` type-widening) were invisible to that command and were only
caught by `npx tsc --noEmit -p tsconfig.app.json` (the actual app
project) and by the full Vitest run respectively. Worth knowing for any
future session: always point `tsc` at `tsconfig.app.json` directly in
this repo, not the root solution file.

**Live sanity check** (against this project's first-configured real SQL
Server connection, `settings.databases[0]` — the `"hr"` HrAutomationDb
database this project's own CLAUDE.md already names as its live
non-default-schema example, not the AdventureWorksDW2025 connection also
configured alongside it): `build_chart_spec` was run directly against two
real queries. (1) `SELECT CAST(Score AS FLOAT) AS Score FROM
recruitment.InterviewFeedbackScore WHERE Score IS NOT NULL` (28 real
rows) produced a real `HISTOGRAM` spec (`title="Distribution of Score"`,
one `Y`-role field, `engine_version="1.0.0"`). (2) `SELECT Status AS
ApprovalStatus, COUNT(*) AS StepCount FROM workflow.ApprovalStep GROUP BY
Status` (`[('Approved', 52), ('Pending', 76)]`) fed through
`compute_analytics_result` first, then `build_chart_spec` with that
result passed in, produced a real `BAR` spec whose
`accessibility.alt_text` — `"Step Count by Approval Status. Pending
ranks first on StepCount at 76 (59.4% of the total)."` — verifiably
contains `analytics.engine`'s own ranking finding's claim text verbatim,
confirming the accessibility-reuse design actually works end-to-end
against real data, not just mocked tests.

**A genuine, disclosed finding from that live check, not a Prompt 15
bug**: this database's real `recruitment.InterviewFeedbackScore.Score`
column is SQL Server `decimal`, which `pyodbc` returns as Python
`decimal.Decimal` — and `agent.result_charting.classify_columns`
(pre-existing, completely unmodified by this prompt) builds its
numeric/date/text classification from the pandas dtype of the result
`DataFrame`; a column of `Decimal` objects gets pandas dtype `object`,
which that function classifies as `"text"`, not `"numeric"`. Since
`build_chart_spec` deliberately reuses `column_types` rather than
re-inferring it (§3's own listed scope boundary — "one column-typing
implementation, not two drifting copies"), an *uncast* `decimal`-typed
single-numeric-column result would be classified as all-text today and
get no chart spec at all (`None`), not a wrong one. This is a limitation
of the pre-existing, untouched `classify_columns`, identically present
(and identically unfixed) in the pre-existing `chart_recommendation`
path this prompt was explicitly told not to modify — named here as an
honest, disclosed finding from live testing, not silently worked around,
and not a regression this prompt introduced.

## 5. Security / tenant-isolation / performance review

- **Security**: `build_chart_spec` is pure computation over already-
  authorized, already-row-capped query results (`/execute`'s own
  existing `validate_sql`/`enforce_row_limit`/RBAC gates run unchanged,
  unmodified, and first) — it reads no new user input, makes no new
  query, and writes nothing. Column/title/accessibility text is built
  entirely from column *names* and already-returned values, never from
  free user text reaching an LLM prompt — there is no injection surface
  this module introduces.
- **Tenant isolation**: not applicable — `build_chart_spec` has no
  notion of tenant/user identity at all; it operates purely on the rows
  `/execute` already fetched under that request's own existing
  authorization check.
- **Performance**: `build_chart_spec` is O(rows + columns) — a handful of
  name-token/regex passes and one pass over the row count for
  downsampling math, no second query, no LLM call. The one real new cost
  at `/execute`'s call site is `compute_analytics_result` (Prompt 13,
  already measured/accepted as cheap, deterministic, zero-I/O in that
  prompt's own review) — now invoked an additional time at `/execute`
  (it already runs once per successful `/ask` via
  `compute_analytics_node`); both call sites operate on the same
  row-capped (`Settings.max_result_rows`) result set, so the added cost
  per `/execute` call is bounded and small, not proportional to anything
  unbounded.

## 6. Known limitations / remaining risks

- No real box-and-whisker or map renderer exists in this build — both
  are detected, classified, and gated off by default, not implemented.
- `visualization_spec` is not yet consumed by the frontend anywhere —
  computed, tested, and exposed via API, same deferred-wiring posture as
  Prompt 13/14's own analytics surfaces.
- Aggregation/format inference is name-pattern-based, not derived from
  the actual SQL that produced the result (no access to the SQL's own
  `SUM(...)`/`AVG(...)` AST at this call site) — a column whose name
  gives no hint (e.g. a generic alias like `col1`) gets no aggregation
  label and `"plain"` format, which is the honest, disclosed fallback,
  not a wrong guess.
- Geography detection is a small, explicitly non-exhaustive name list
  (the same honesty convention `db/relationship_inference.py`'s own
  heuristic lists already establish) — a real geography column with an
  unrecognized name (e.g. just `"loc"`) is simply never tagged, never a
  false positive from guessing too aggressively, but also never a chart.
- The `Decimal`-vs-`classify_columns` limitation found during live
  testing (§4) is real, disclosed, and unfixed — it predates this prompt
  and affects the pre-existing `chart_recommendation` path identically;
  fixing it would mean modifying `agent/result_charting.py`, which this
  prompt's own design explicitly keeps untouched (§3).

## Recommended next prompt

Wire `visualization_spec` into the frontend (`ChartSection.tsx`/
`ChartPicker.tsx` reading its richer title/accessibility text as the
actual chart title/ARIA label instead of the client's own simpler
`_build_title`-equivalent string), or move on to Prompt 16 per the
master initiative's own sequencing.
