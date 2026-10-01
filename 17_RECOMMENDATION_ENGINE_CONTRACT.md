# 17 — Evidence-First Recommendation Engine

Prompt 17 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`16_FORECASTING_CONTRACT.md`. One new backend module
(`recommendation/engine.py`), additive `recommendation/models.py`
extensions, additive `agent/state.py`/`agent/graph.py`/`agent/nodes.py`
wiring (a new, straight-edge graph node — never a conditional one, never
able to short-circuit the graph), additive `config.settings`/
`api/schemas.py`/`api/main.py` surface. No existing node, route, or
response field changed shape; every new field is optional/defaulted, and
every caller that predates this prompt — in particular
`analytics.forecasting.recommendations_for_forecast`'s six pre-existing
`Recommendation(...)` call sites — keeps working unchanged.

## 1. Inspection: what already existed vs. the real gap

`recommendation/models.py` already defined `Recommendation`/
`RecommendationKind` (a minimal `kind`/`claim`/`rationale` shape) and
`recommendation/provider.py` an unimplemented `RecommendationProvider`
Protocol — `01_BASELINE_ARCHITECTURE_AND_REUSE_INVENTORY.md`'s own
inspection had found no recommendation logic anywhere to adapt. That
changed once, narrowly, in Prompt 16: `analytics.forecasting
.recommendations_for_forecast` became the first real producer of this
type, but deliberately as a standalone function tied to one input shape
(`ForecastResult`), not a general-purpose engine.

Four other modules already compute real, typed, evidence-grade facts
with nowhere to turn them into actionable recommendations:
`analytics.engine.compute_analytics_result` (anomalies, column-quality
stats, growth, rankings — all `DataTruthLevel.DATABASE_FACT`),
`analytics.root_cause.investigate_root_cause` (validated contributors,
already evidence-gated by its own `root_cause_min_contribution_percent`
bar), `db.query_cost.estimate_query_cost` (plan-based cost estimates),
and `observability.metrics.PerformanceMetrics.snapshot` (live per-stage
timing rollups). `config.sensitive_columns`/`agent.sql_validator
.find_restricted_column_references` already classify and detect
restricted-column access, but purely as a pre-execution block — nothing
surfaces that fact afterward as something a reviewer should see. Grep
confirmed zero cross-cutting recommendation logic connecting any of these
to the other before this prompt.

**The one real design decision, made explicitly rather than assumed:**
master rule 10 ("never silently convert AI inference into confirmed
business truth") and this prompt's own "no recommendation without
evidence" requirement are in tension with master rule 4 ("preserve
backward compatibility") the moment a `Recommendation.evidence` field is
added at all — `recommendations_for_forecast`'s six existing call sites
construct `Recommendation`s with none. Resolved by making "no
recommendation without evidence" a **pipeline-level** invariant
(`recommendation.engine._finalize_candidate` is the only place this
module ever constructs one, and it refuses to for an empty-evidence
candidate), not a `Recommendation` type-level requirement — the type
itself stays permissive (`evidence` defaults to `()`), so a pre-existing
producer is untouched, while every candidate *this* engine produces is
provably evidence-backed by construction, not by convention.

## 2. What was built

### `recommendation/models.py` (additive)

- `RecommendationCategory` — the nine required categories (`performance`,
  `anomaly`, `revenue`, `customer`, `product`, `operations`,
  `data_quality`, `security`, `database_performance`), `None` for a
  pre-Prompt-17 recommendation.
- `Recommendation` gained `category`, `evidence: tuple[ProvenancedClaim,
  ...]`, `affected_entity`, `action`, `measurable_impact`, `confidence`
  (`Field(ge=0.0, le=1.0)`), `rule_or_model`, `limitations`,
  `generated_at` (a real timestamp — deliberately *not* omitted the way
  `ForecastModelMetadata` omits one for pure-function reproducibility,
  since a recommendation is a point-in-time judgment about live data, not
  a reproducible closed-form calculation), and `engine_version` — every
  one optional/defaulted.
- A new `model_validator`: any `evidence` item present must be
  `DATABASE_FACT` or `CONFIRMED_BUSINESS_TRUTH`, never `AI_INFERENCE` —
  an inference cannot ground another inference without defeating the
  entire evidence-first premise. This is the one new **type-level**
  invariant; "evidence must be non-empty" stays pipeline-level (see §1).

### `recommendation/engine.py` (new)

`generate_recommendations(inputs: RecommendationInputs, settings=None) ->
tuple[Recommendation, ...]` — the pipeline: **Data → Finding → Evidence →
Rule/Model → Candidate → Evidence Validation → Confidence →
Recommendation**. Zero new queries, zero new LLM calls — every rule
consumes a typed result some other, already-existing module already
computed deterministically.

| Category | Rule | Input (already computed by) |
|---|---|---|
| `anomaly` | `AnomalyRule` | `analytics.engine`'s own `ANOMALY` findings (→ `analytics.anomaly.detect_anomalies`) |
| `data_quality` | `HighNullRateRule` | `analytics.engine`'s `COLUMN_SUMMARY` findings |
| `revenue` | `DecliningRevenueRule` | `analytics.engine`'s `GROWTH` findings, keyword-gated |
| `customer` | `CustomerConcentrationRule` | `analytics.engine`'s `RANKING` findings, keyword-gated |
| `product` | `ProductConcentrationRule` | `analytics.engine`'s `RANKING` findings, keyword-gated |
| `operations` | `OperationsRootCauseRule` | `analytics.root_cause.investigate_root_cause` |
| `database_performance` | `HighCostQueryRule` | `db.query_cost.estimate_query_cost` |
| `performance` | `SlowStageRule` | `observability.metrics.PerformanceMetrics.snapshot` |
| `security` | `RestrictedColumnExposureRule` | `config.sensitive_columns` + `agent.sql_validator.find_restricted_column_references` |

(`customer`/`product` deliberately share one generic `_concentration_candidates`
rule function, parameterized by keyword list/category/rule name, rather
than two near-duplicates — master rule 2/3.)

1. **One construction point, two gates.** `_finalize_candidate` drops a
   candidate with empty `evidence` (**"no recommendation without
   evidence," enforced in code**) or with `confidence` below
   `Settings.recommendation_min_confidence` (**the literal "supported vs.
   unsupported" distinction**, independently testable from evidence
   presence) before ever constructing a `Recommendation`.
2. **Authorization, re-checked here too.** A candidate whose evidence
   touches a column `config.sensitive_columns` classifies restricted is
   suppressed outright (never partially redacted) unless
   `RecommendationInputs.caller_roles` holds
   `agent.authz.Permission.VIEW_RESTRICTED_COLUMNS` — checked against the
   *viewer* of this recommendation set, independent of whatever role the
   original query ran under (defense in depth, following `agent/authz.py`'s
   own stated "re-check independently" principle). Not SECURITY-category-
   specific: any category's candidate referencing a restricted column is
   suppressed the same way. A suppression is logged via
   `security.audit_log.log_security_event`.
3. **Column-name keyword heuristics** (revenue/customer/product) are a
   disclosed, bounded approach — no business-glossary lookup — matching
   this codebase's existing precedent (`agent.complexity`'s regex signals,
   `analytics.visualization.detect_geography_columns`'s name matching).
   Every keyword-gated candidate discloses this in its own `limitations`.
4. **Confidence formulas are deterministic and disclosed**, not
   statistically derived (matching `analytics.forecasting`'s own
   round-number-threshold honesty) — e.g. anomaly confidence scales with
   how many of the five detection methods agree; database-performance
   confidence is `0.9`/`0.6` for high/moderate severity.
5. **Tenant isolation**: this module has no tenant concept and needs
   none — it is a pure function over data already scoped to one resolved
   database connection/one request, persists nothing. A materially
   narrower surface than `onboarding/`/`semantic/catalog.py`, which *do*
   carry a real `tenant_id` because they persist cross-request state; if
   a future prompt persists recommendations for later, cross-viewer
   retrieval, that storage layer would need the identical pattern those
   two already establish.

### `agent/state.py`, `agent/graph.py`, `agent/nodes.py` (additive wiring)

- `AgentState["recommendations"]` (`list[dict]`, output) — one dict per
  `Recommendation.model_dump()`, `[]` (never `None`) when nothing fired,
  the feature was off, or computation failed unexpectedly.
- `generate_recommendations_node`, wired `generate_forecast →
  generate_recommendations → generate_insight` — **a straight edge on
  both sides**, the identical structural non-short-circuit proof
  Prompts 11/12/16 already established for their own new nodes. Feeds the
  engine whichever typed evidence this request already has in hand:
  `state["analytical_result"]` (reconstructed into the typed
  `AnalyticsResult`), `state["cost_estimate"]` (already the typed object),
  a live `observability.metrics.PerformanceMetrics.snapshot()`, and any
  restricted-column hit in the final executed SQL (re-detected via the
  exact `agent.sql_validator.find_restricted_column_references` function
  `validate_sql_node` already uses — no second detection implementation).
  `root_cause_result` is never supplied from this live node — this
  single-query pipeline never produces the second, comparison dataset
  `analytics.root_cause.investigate_root_cause` needs (Prompt 14's own
  disclosed gap, inherited rather than worked around); the `operations`
  category therefore never fires live, only from a direct,
  standalone call to `recommendation.engine.generate_recommendations`.
  Fails open on any unexpected error, the same posture every other
  accuracy-aid node in this graph already takes.
- The graph now has **eighteen nodes** (see "Self-correcting retry loop"
  in `CLAUDE.md` for the full, current list).

### `config/settings.py` (5 new settings)

`enable_recommendation_engine` (default `True` — a pure,
zero-I/O-beyond-already-computed-evidence computation, the same
"operator escape hatch, on by default" posture as
`enable_analytics_engine`), plus four tunables:
`recommendation_min_confidence` (default `0.5`),
`recommendation_null_rate_threshold` (default `0.2`),
`recommendation_concentration_share_threshold` (default `50.0`),
`recommendation_slow_stage_ms_threshold` (default `2000.0`).

### `api/schemas.py` / `api/main.py` (response surface)

`AskResponse.recommendations: list[dict[str, Any]]` — one dict per
`Recommendation.model_dump()`, the identical raw-dict-of-a-`model_dump()`
convention `analytical_result` already established in Prompt 13 (not the
fully-typed-mirror approach `forecast_result` uses — the recommendation
shape already carries its own rich typed fields and a client-facing
schema duplication would add nine-plus near-identical fields for no
additional validation this engine's own Pydantic model doesn't already
provide). Always `[]`, never a sentinel.

## 3. Testing

`tests/test_recommendation_engine.py` (new): one class per category rule
— each with an explicit **supported** case (evidence clears, confidence
clears, a `Recommendation` is produced) and **unsupported** case (the
rule's own trigger condition isn't met, nothing is produced) — plus
dedicated classes for authorization (an unauthorized viewer never sees a
restricted-column-evidenced recommendation, in *any* category, not just
`security`; an authorized one does), evidence validation and the
confidence floor (white-box tests of `_finalize_candidate` proving a
no-evidence candidate and a below-floor-confidence candidate are both
dropped, and a candidate clearing both is not), and the feature flag/
empty-input degenerate cases.

`tests/test_recommendation.py` (extended): the new `RecommendationCategory`
enum's completeness, the new `evidence` field's type-level validator
(`DATABASE_FACT`/`CONFIRMED_BUSINESS_TRUTH` accepted, `AI_INFERENCE`
rejected), `confidence`'s range validation, and an explicit regression
test that `analytics.forecasting.recommendations_for_forecast`'s exact
pre-existing construction shape still validates with every new field
defaulting as expected.

`tests/test_agent_nodes.py` (+~100 lines) covers
`generate_recommendations_node`'s own skip/attempt/fail-open gates (flag
off, a genuine anomalous result, an empty result, a restricted-column
reference producing a `security` recommendation, an unexpected
computation error) and that it runs before `generate_insight_node`
without interfering with it. `tests/test_sql_agent_integration.py`
(updated + extended): the pre-existing `generate_forecast`-sits-before-
`generate_insight` structural test now asserts `generate_forecast →
generate_recommendations` (its new, real downstream neighbor), and a new,
symmetric structural test proves `generate_recommendations →
generate_insight` is itself a straight edge on both sides.

**Found and fixed during this pass's own verification, not
pre-existing:** two mypy narrowing errors in
`_concentration_candidates` (`top.share_percent: float | None` wasn't
narrowed across a re-access of the same attribute via a different
variable — fixed by checking `top.share_percent is None` directly on the
already-bound `top`, not on `ranking.entries[0]` a second time) and a
`get_connection(settings, state.get("selected_database"))` call in the
new `_restricted_column_hits_in_sql` helper that didn't fall back to
`"default"` for a `None`/single-database setup, unlike every other
multi-database-aware call site in `agent/nodes.py` — both found and
fixed before merge, not left as known gaps. Full regression suite reran
clean after every fix: 3172/3172 backend tests passing (up from 3132
before this prompt), 0 new mypy errors outside test files, 0 ruff
findings, `black --check` clean.

## 4. Known, disclosed limitations (not oversights)

- **`operations` (root-cause-based) recommendations never fire from the
  live graph node** — inherited directly from Prompt 14's own disclosed
  gap (`analytics.root_cause.investigate_root_cause` needs a second,
  comparison dataset this single-query pipeline doesn't produce). Fully
  built, fully tested, callable directly; wiring it live is the same
  "needs a second query" follow-up `14_ANOMALY_ROOT_CAUSE_CONTRACT.md`
  already named.
- **Revenue/customer/product rules are column-name keyword heuristics,
  not a business-glossary lookup** — `semantic/catalog.py`'s governed
  business concepts (Prompt 9/10) are a natural future evidence source
  for these three categories specifically (a published `METRIC`/
  `DIMENSION` catalog entry would replace the keyword list with a real,
  governed classification), not attempted in this pass.
- **The `performance`/`database_performance` categories' confidence
  formulas are deterministic but not statistically calibrated** — `0.9`/
  `0.6` for cost severity, a linear scale against the configured
  threshold for stage latency — disclosed as such, not presented as a
  measured probability.
- **No persistence, no cross-request/cross-viewer recommendation
  history** — every call is a pure function over one request's already-
  computed evidence; a future "recommendation feed" or "export this
  report" feature would need the identical `tenant_id` + policy-module
  pattern `onboarding/`/`semantic/catalog.py` already establish, which
  this module deliberately does not invent ahead of a real requirement.
- **No frontend changes in this pass** — `AskResponse.recommendations` is
  real, typed-at-the-model-level API surface today; nothing in
  `frontend/src` reads it yet, the same deferred-wiring posture Prompts
  14/15/16 already established for their own new surfaces.
