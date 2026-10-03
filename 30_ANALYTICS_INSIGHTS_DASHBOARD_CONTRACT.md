# PROMPT 30 — Analytics & Insights Dashboard

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Build the primary business-user analytics experience using real analytics APIs.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Inspect current chat/question UI, analytics APIs, result engine, visualization specifications and frontend chart components.

### Implementation requirements
Support natural-language questions and display KPI/answer, visualization, explanation, filters, time range, freshness, evidence, authorized SQL details, anomalies and related questions. Include KPI cards, trends, comparisons, rankings, distributions, anomaly panels and drill-down. Provide loading, empty, error and insufficient-evidence states. Respect field-level permissions. Link insights to evidence and semantic definitions.

### Testing requirements
Test business-user access, unauthorized fields, real API data, empty/error states and visualization rendering.

### Acceptance criteria
A business user can complete the full question→analysis→evidence experience from the UI.

### Non-functional requirements
- Preserve existing functionality.
- Avoid duplicate implementations.
- Enforce authentication, authorization and tenant isolation.
- Never allow the LLM to bypass deterministic security controls.
- Keep secrets out of source code and logs.
- Add structured logging/tracing using the existing observability framework.
- Keep APIs backward compatible unless a documented migration is required.
- Update documentation and configuration examples.

### Required execution lifecycle
INSPECT → PLAN → IMPLEMENT → TEST → REVIEW → FIX → DOCUMENT → REPORT

Before broad implementation, present the implementation plan. After implementation, run relevant tests and fix regressions.

### Required final report
Report:
1. Areas inspected.
2. Existing functionality reused.
3. Files created/modified.
4. API/config/database changes.
5. Tests added.
6. Tests executed and results.
7. Security findings.
8. Tenant-isolation findings.
9. Performance implications.
10. Known limitations.
11. Remaining risks.
12. Recommended next prompt.

## Outcome summary

Prompt 30 turned the analytics the platform already computed into a
business-user analysis view inside each answered question. **No new
dashboard page was built.** The question→answer flow already exists
(`frontend/src/components/chat/TurnCard.tsx`), and the master contract's
"never duplicate dashboard functionality" rule makes a parallel page the
wrong shape. The analysis renders in the answered turn, under the insight,
gated exactly like the insight itself (after "Confirm and Run", and only
while the SQL box still holds the SQL that produced the answer).

### What was actually missing

The backend already computed most of this. The frontend never received it:

- `AskResponse` carried `analytical_result`, `forecast_result`, and
  `recommendations`, but `frontend/src/lib/types.ts` declared none of them.
- `AgentState` held `analytical_plan`, `analytical_intent`, and
  `governing_metrics`, and `api/main.py::_ask_response_from_state` dropped all
  three. The dashboard's filters, time range, intent, and semantic
  definitions had no path to the client.
- `ExecuteResponse.visualization_spec` (Prompt 15) had no frontend type.
- The `X-Cache` result-cache signal (Prompt 22) was a response header that
  `request()` discards. The UI had no way to tell a served-from-cache result
  from a live one.
- Restricted-column failures (`last_error_category == "restricted_column"`)
  surfaced only as generic retry text.

### What changed

**Backend (additive, pass-through only, no computation changed):**

- `api/schemas.py`: typed `AnalyticalIntentOut`, `AnalyticalPlanOut` (with
  its sub-models), and `GoverningMetricOut`. Four new `AskResponse` fields,
  each defaulting to "nothing to report": `analytical_intent`,
  `analytical_plan`, `governing_metrics`, `restricted_field_notice`.
  `ExecuteResponse.cache_status` (`"hit"`/`"miss"`/`null`).
- `api/main.py`: `_analytical_intent_out`, `_analytical_plan_out`,
  `_governing_metrics_out` convert the existing state dicts. The restricted
  notice is a presentation-only translation of an already-computed failure
  category (`status == "failed"` and `last_error_category ==
  "restricted_column"`). The restricted-column check, its retry behavior, and
  what it blocks are unchanged. `/execute` sets `cache_status` next to the
  existing `X-Cache` header writes, from the same `cache_status` local.

**Frontend:**

- `lib/types.ts`: mirrors the above, plus the analytics result, forecast,
  and recommendation shapes the panels read (`TruthLevel`,
  `ProvenancedClaim`, `GrowthStat`, `RankingStat`, `AnomalyPoint`,
  `ForecastResult`, `Recommendation`, and related types).
- `lib/analyticsCharts.ts` (new, DOM-free): typed-stat-to-`PreparedChart`
  adapters, `deriveKpis`, `sanitizeLabel`, and `drillDownQuestion`. Every
  database-originated label goes through `sanitizeLabel` before it is shown
  or fed back into a question (master rule 8).
- `components/analytics/` (new): `AnalyticsSummary` (the orchestrator) plus
  `KpiStrip`, `TrendPanel`, `RankingPanel` (bars or share/doughnut),
  `ComparisonPanel`, `AnomalyPanel`, `ForecastPanel`, `EvidencePanel`,
  `GoverningMetricsPanel`, `QueryScopePanel`, `TruthLevelBadge`, and the
  shared `AnalyticsPanel` shell.
- `components/sql/KpiCard.tsx`: extended in place with optional `delta` and
  `caption`. Existing callers pass neither and render exactly as before.
- `components/chat/TurnCard.tsx`: renders `AnalyticsSummary` under the
  insight, lazy-loaded, gated like the insight. Shows the restricted-field
  notice in the failed-status branch.
- `store/chatStore.ts`, `lib/history.ts`: `confirmedCacheStatus` is carried
  from the confirmed `/execute` response. It is session-only.
- `hooks/useChartPalette.ts`: theme-aware palette, following `ResultChart`'s
  own subscription pattern.

### Reuse (master rule 2/3)

- `ResultChart` / `chartEngine`'s `PreparedChart` renderer: every chart in
  the analysis is rendered by the same component the SQL chart picker uses.
  No new chart library, no second chart engine.
- `KpiCard`: extended, not forked.
- `Expander`, `Badge`, `Markdown`: existing UI primitives.
- `chatStore.askQuestion`: drill-down and related questions re-enter the
  normal `/ask` path. No new query engine, no new route.
- The backend's own `recommendation.engine` output is shown as-is, not
  re-ranked or re-derived on the client.
- `TurnCard`'s existing lazy-loading boundary: the analysis joins the same
  chunk boundary as `ChartSection` (see Known limitations for why this
  matters).

### How the requirements map

- **KPI cards / trends / comparisons / rankings / distributions / anomaly
  panels / drill-down**: `KpiStrip`, `TrendPanel`, `ComparisonPanel`,
  `RankingPanel` (`bars` for rankings, `share` for distributions), and
  `AnomalyPanel`. The intent classification
  (`analytical_intent.intent`) chooses which view leads: a distribution
  question gets the share view, a comparison gets the side-by-side. Every
  panel the data supports is still shown.
- **Filters, time range**: `QueryScopePanel`, a read-only expander over the
  validated `analytical_plan` (metrics, breakdown dimensions, filters, time
  window, grain, ranking, row limit). It cannot change the query.
- **Freshness**: a header line with the source database, row count, and
  "Served from a recent cached result" or "Live query result". The label
  comes from the `/execute` cache signal.
- **Evidence**: each claim shows its truth level (`TruthLevelBadge`). Each
  recommendation's evidence is listed with its own level. Every
  recommendation is labelled an AI estimate.
- **Authorized SQL details**: the existing SQL editor and "Confirm and Run"
  (unchanged). Restricted columns are never displayed or bypassed.
- **Anomalies**: the existing deterministic detector's output
  (`AnomalyPoint`/`AnomalySignal`), with each method's own formula and
  threshold shown.
- **Related questions**: `next_question` recommendations as one-click
  buttons, which submit through `askQuestion`.
- **Semantic definitions**: `GoverningMetricsPanel` shows each approved
  metric's expression and description, labelled `CONFIRMED_BUSINESS_TRUTH`.
- **Loading**: the existing `PendingTurn` state, plus a lazy-load fallback.
- **Empty**: "No rows matched this question". The section does not render
  panels.
- **Error**: the existing `failed` branch, now with the field-permission
  notice when the cause is a restricted column.
- **Insufficient evidence**: skipped calculations are listed
  (`insufficient_data_reasons`). A rejected forecast shows its own rejection
  reasons. A result with no breakdown says so.
- **Field-level permissions**: a restricted column, with retries exhausted,
  now produces a plain-language notice explaining that the answer needs
  access. The enforcement is untouched (the restricted-column gate in
  `validate_sql_node`, its retry, and the recommendation-engine suppression
  from Prompt 17 all still apply).

### Testing

- **Backend** (`tests/test_api_ask.py::TestAnalyticsFieldPassThrough`,
  `tests/test_api_execute.py::TestResultCache`): the four new `AskResponse`
  fields pass through with their typed shape. They default to empty for a
  state that predates them. A restricted-column failure sets the notice.
  An unrelated failure does not. `cache_status` mirrors `X-Cache` on a
  miss, a hit, and when the cache is disabled.
- **Frontend pure logic** (`lib/analyticsCharts.test.ts`, 15 tests): label
  sanitizing (control characters, length cap), the drill-down template
  against a hostile multi-line label, the adapters (with truncation and the
  "Other" fold), and KPI derivation (including suppressing a total when the
  ranking was row-capped, so no misleading partial sum appears).
- **Frontend components** (`components/analytics/AnalyticsSummary.test.tsx`,
  16 tests, against backend-shaped fixtures in `src/test/analyticsFixtures.ts`):
  real API-shaped data through every panel. Intent-driven panel choice. The
  drill-down and related-question callbacks, and the read-only fallback when
  no callback is given. Governed definitions and recommendation evidence
  with their truth levels. Rejected and successful forecasts. A quiet
  anomaly check. The query-scope expander, including the classifier's
  ambiguity flags. The empty, no-breakdown, cached/live, and row-capped
  states.
- **TurnCard integration** (`components/chat/TurnCard.analytics.test.tsx`,
  5 tests): the analysis is hidden until confirmation, renders with its
  freshness after it, is hidden once the SQL box has been edited away from
  the answer's SQL, and the restricted-field notice shows only for that
  failure.
- **Visualization rendering**: exercised through the real `ResultChart`
  (the existing jsdom canvas polyfill), asserting each chart's accessible
  name (`line chart`, `bar-horizontal chart`, `doughnut chart`).

**Results:** backend 3541/3541 (`pytest`, full suite, run twice; one
earlier run failed on a test that passes in isolation and in every pairwise
combination, and the rerun on an unchanged tree passed in full, see
Known limitations for the concurrent-edit note). Frontend 386/386
(`vitest run`, up from 349). `tsc -b` clean. `oxlint` reports no warnings
in any file this prompt wrote. `npm run build` passes. `ruff check`, `black
--check`, and `mypy` are clean on every backend file this prompt touched.

### Known limitations

- **No new cross-tenant or cross-user exposure surface.** Every field this
  prompt surfaces was already returned by `/ask` or `/execute` for that
  same caller. Nothing new is read, nothing is re-scoped, and the analysis is
  computed from the caller's own already-authorized result. Tenant isolation
  is unchanged by construction.
- **Reloaded past turns show no analysis.** The analytics fields are not part
  of the persisted message metadata, so a turn reopened from chat history
  shows its answer and SQL but no analysis panels. This is the same
  disclosed reconstruction gap as the rest of `history.ts`'s reloaded-turn
  path (CLAUDE.md, chat history). Persisting `analytical_result` and
  `analytical_plan` is a real follow-up.
- **`cache_status` is session-only.** Like every other `confirmed*` field,
  it is not restored on reload. It is set only from the live `/execute`
  response.
- **`visualization_spec` is not consumed.** Prompt 15's richer chart spec
  (`ExecuteResponse.visualization_spec`) remains unrendered on the frontend,
  as Prompt 15 deliberately left it. The analysis panels use the typed
  `analytical_result` stats directly. Wiring it in means extending the
  persisted confirmed-result path as well, a larger change than this prompt
  took on.
- **Root-cause attribution is absent.** `analytics.root_cause` is not wired
  into any live graph node (Prompt 14's disclosed scope), so the dashboard
  does not show "why" a change happened. It shows what changed and which
  periods were flagged.
- **Filters are displayed, not editable.** The query scope is read-only. A
  business user cannot adjust a filter or time window from the panel. Doing
  so would mean re-entering the question path with a changed plan, a
  separate design.
- **Copy is English-only.** The new panel text is not translated. The nav
  and existing UI are translated, matching Prompts 26-29's precedent for new
  pages.
- **Drill-down is template-based.** A ranking row submits a fixed question
  ("Show the details behind X for Y") built from sanitized labels. It is not
  a true data drill-down into rows. A drill-down that returns more detail is
  a natural next step.
- **Single-series anomaly and trend views.** A time series with several
  value columns renders the first one. The backend only computes one growth
  stat per result today.
- **Recommendation feedback is not offered here.** Recommendations are
  displayed, not rated. Feedback and resolution remain on the Prompt 18
  routes, which the Tenant Admin dashboard already lists read-only.

### Known risks

- **Lazy-load regression risk.** The analysis is lazy-loaded so Chart.js stays
  out of the main bundle. A later change that statically imports any
  analytics panel would silently pull Chart.js into every page load. The
  production build's chunk-size warning is pre-existing and does not catch
  this.
- **Concurrent edits during a test run.** While the backend suite was
  running, several files in the working tree (`api/schemas.py`,
  `api/tenant_admin*.py`, `security/tenancy.py`, and others) were modified
  outside this session. One run failed on a schema-refresh response carrying
  an extra `error` key, which is not in the current code. A clean rerun on the
  unchanged tree passed 3541/3541. Anything that edits the tree during a
  run can produce a result that matches no single committed state.

### Recommended next prompt

Prompt 31. Before it starts, reconcile the working tree: the files listed
above were changed outside this prompt, and they should be reviewed and
either committed or reverted deliberately, not carried into the next prompt
by accident.
