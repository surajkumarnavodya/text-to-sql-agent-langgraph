# 12 — Analytical Planning Layer

Prompt 12 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`11_ANALYTICAL_INTENT_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/TEST/
REVIEW/FIX/DOCUMENT** — two new modules (a typed contract + a
deterministic validator), one new LangGraph node sitting between
`classify_analytical_intent` and `plan_query`, a small, non-duplicative
`plan_query_node` gate addition, additive `generate_sql_from_llm`/
`config.settings` surface, and a DDL-parsing refactor that removes one
real duplicate implementation. No existing route, node, or `Settings`
field changed behavior for a question that doesn't trigger planning, has
planning disabled, or whose structured plan fails validation.

## 1. Inspection: what already existed vs. the real gap

`agent.nodes.plan_query_node` (Prompt 00-era) already produces an
up-front plan for a non-trivial question, but it is **free text** —
`AgentState["query_plan"]: list[str]` — and the only thing that ever
checks it against the generated SQL is a *second LLM call*
(`review_sql_against_plan_from_llm`). Nothing in the codebase before this
prompt deterministically verified that a plan's own metric/dimension/
filter/time references were real, authorized, joinable, or supported by
the target database — the exact gap `agent.sql_validator.validate_sql`
already closed for *generated SQL itself* (an AST-based allowlist, not a
second LLM judgment) was open one layer earlier, for the *plan*.
`agent.intent.AnalyticalIntentClassification` (Prompt 11) gets closer —
it extracts `metric_candidates`/`dimensions`/`time_requirement`/
`comparison`/`filters` from the question's own text — but it's a
*classification*, not a resolvable-to-real-schema structure, and
`agent.nodes.plan_query_node`'s only consumer of it is a gate/prompt
hint, never a validated object.

`db/relationship_inference.py` (Prompt 07) already had the exact
type-family classification (`_TYPE_FAMILY_KEYWORDS`/`_type_family`) this
prompt's time-feasibility check needed, and `db/adapter.py` (Prompt 03/04)
already had a `DatabaseCapabilities` object for *connection*-level
capability facts — but no per-dialect-SQL-feature support table existed
anywhere (window functions/CTEs/PERCENTILE_CONT/STRING_AGG support vary
by `DB_TYPE`, and nothing tracked that). `agent/nodes.py`'s own
`_extract_column_names` (a DDL-text column-name extractor) was a private,
single-use helper with no public counterpart `agent.plan_validator` could
reuse without duplicating it.

`agent.authz.has_role_permission`/`config.sensitive_columns
.load_sensitive_columns` (2026 Phase 2 security review) already gate a
restricted column at the *generated-SQL* layer (`validate_sql_node`) —
confirmed reusable verbatim as an *earlier*, additional layer for a
*plan's* column references, never a replacement for that existing gate.

## 2. What was built

### Typed contract — `agent/analytical_plan.py` (new)

Mirrors `agent/intent.py`'s "typed contract module, separate from the
LLM-calling module and the validation module" split:

- `PlanAggregation`/`PlanSortDirection` — closed enums.
- `PlanMetric`/`PlanDimension`/`PlanFilter`/`PlanTimeRange`/
  `PlanComparison`/`PlanRanking`/`PlanSort` — small, frozen `BaseModel`s,
  the shape family a structured plan is built from. `PlanMetric.table`/
  `.column` are the only fields in the whole contract allowed to be
  `None` — exactly when `governed_metric_key` names an already-published,
  `CONFIRMED_BUSINESS_TRUTH` governed metric (`AgentState
  ["governing_metrics"]`, Prompt 10) instead.
- `AnalyticalPlan` — `metrics`/`dimensions`/`filters` (tuples),
  `time_range`/`grain`/`comparison`/`ranking`/`sort`/`limit`/
  `required_operations`, `truth_level: DataTruthLevel = AI_INFERENCE`
  (reuses `agent.provenance`, never promoted — identical convention to
  `agent.intent`). Every field defaults to empty/`None`.
- `render_plan_as_steps(plan) -> list[str]` — turns a structured plan
  back into the same step-string shape `plan_query_node`'s free-text
  planner already produces, **reused by `review_sql_node`'s existing
  plan-conformance LLM check** rather than adding a second review node
  (master-contract rule 3).

No LLM call lives here — `agent.llm_client.generate_analytical_plan_from_llm`
makes the call, `agent.plan_validator.validate_plan` re-verifies it,
`agent.nodes.build_analytical_plan_node` wires both into the graph.

### Deterministic validator — `agent/plan_validator.py` (new)

`validate_plan(plan, schema_tables, governing_metrics, caller_roles,
db_type) -> PlanValidationResult` — a pure function, no I/O, every check
runs (violations are never short-circuited at the first one found):

1. **Metric/dimension existence** — every `(table, column)` pair the
   plan names (metrics unless `governed_metric_key`'d, dimensions,
   filters, time range) must resolve to a real table/column in
   `schema_tables` (the already-retrieved top-k tables for this
   question — the identical boundary `generate_sql`'s own prompt is
   already scoped to). A `governed_metric_key` must match a name in
   `governing_metrics`; a metric with neither a table/column nor a valid
   `governed_metric_key` is `incomplete_metric`. A plan with zero
   metrics is `no_metrics`.
2. **Relationship paths** — when the plan touches more than one table, a
   BFS over an adjacency graph built from `FOREIGN KEY (...)
   REFERENCES ...` edges **parsed out of the already-retrieved
   schema_tables' own DDL text** (new `db.schema_introspection
   .extract_ddl_foreign_key_targets`) must connect every pair, or it's
   `no_relationship_path`. Disclosed, bounded scope: only FK edges
   visible in DDL already shown to the LLM are considered — never a
   full cross-schema graph search, and never an inferred (non-FK)
   candidate relationship (`db/relationship_inference.py`'s own,
   separate, lower-confidence kind of claim).
3. **Filter/sensitive-field policy** — any referenced column classified
   `"restricted"` (`config.sensitive_columns.load_sensitive_columns`)
   without the caller holding `Permission.VIEW_RESTRICTED_COLUMNS`
   (`agent.authz.has_role_permission`) is `restricted_column` — the
   identical gate `validate_sql_node` already applies to generated SQL,
   reused (not duplicated) as an earlier, additional layer.
4. **Time feasibility** — a `time_range` column (once confirmed to
   exist) must classify as the `"datetime"` type family (`db
   .relationship_inference.type_family`, promoted from private to
   public specifically for this reuse) or it's `infeasible_time_range`.
5. **Database capabilities** — each tag in `required_operations` that
   appears in a small, honestly-scoped `_OPERATION_SUPPORT` table (six
   tags: `window_function`/`lag_lead`/`cte`/`percentile_cont`/
   `string_agg`/`rollup`) is checked against `db_type`; a *known*
   incompatibility (e.g. `percentile_cont` + `mysql`) is
   `unsupported_operation`. An unrecognized tag is never blocked — fail
   open on ignorance, fail closed only on a documented incompatibility.

`PlanValidationResult(is_valid, violations: tuple[PlanViolation, ...])`
— `is_valid=False` the moment any violation exists; the full list is
always returned for logging/debugging.

### Refactor — DDL-parsing helpers promoted to `db/schema_introspection.py`

`agent.nodes._extract_column_names` (DDL text → column names) moved to
`db.schema_introspection.extract_ddl_column_names` (public); `agent/
nodes.py` now imports it under the same private name
(`_extract_column_names = extract_ddl_column_names`) so
`_suggest_correct_column`'s own call sites and docstring references are
unaffected — one implementation, two callers (`_suggest_correct_column`
and `agent.plan_validator`), not a duplicate. New sibling
`extract_ddl_foreign_key_targets` (DDL text → referred table names) has
no prior single-caller implementation to deduplicate; it's genuinely new,
narrowly scoped to table-level path existence (not column-level join
correctness, which `agent.sql_validator` already checks on the real
generated SQL). `db.relationship_inference._type_family` was renamed to
the public `type_family` (same body, same internal call sites updated)
for the identical "promote an existing private helper for cross-module
reuse" reason.

### LLM call + parsing — `agent/llm_client.py` (extended)

`_ANALYTICAL_PLAN_SYSTEM_PROMPT` (the standard DATA-framing/security
boilerplate every system prompt in this file repeats, plus the full
`AnalyticalPlan` JSON shape spelled out field-by-field, with an explicit
"every table/column name MUST be copied exactly from the Schema section"
rule — the single most important instruction for a plan that's about to
be deterministically checked against that exact schema).
`_build_analytical_plan_user_prompt` **reuses** (never re-implements)
`_build_plan_relationship_block`/`_build_plan_business_concept_block`
(already built for `generate_query_plan_from_llm`) plus
`_build_intent_governing_metrics_block` (already built for Prompt 11's
classification call) for the metric-name grounding. `_parse_analytical_plan_response`
mirrors `_parse_intent_response`'s exact fail-open shape: JSON parse →
`AnalyticalPlan.model_validate` → `None` on any `ValidationError`/
`JSONDecodeError`. `generate_analytical_plan_from_llm` clones
`generate_query_plan_from_llm`'s call shape (`temperature=0.0`, new
`Settings.analytical_plan_max_tokens`, `OllamaUnavailableError` re-raise,
`_log_ollama_timing`).

`_build_analytical_plan_block(plan: dict) -> str` renders a **validated**
plan via `agent.analytical_plan.render_plan_as_steps` (reused, not
re-rendered a second way) with framing that states plainly the plan has
already been deterministically re-checked against the live schema,
sensitive-column policy, and the target database — distinct from, and
more authoritative-sounding than, the ordinary advisory `_build_plan_block`.
`generate_sql_from_llm`/`_build_user_prompt` both gained an
`analytical_plan: dict | None = None` parameter; `_build_user_prompt`
shows **either** the generic `query_plan` block **or** the stronger
`analytical_plan` block (never both — avoiding showing the model the
identical steps twice under two different framings), while `query_plan`
remains set on state regardless so `review_sql_node` keeps working.

### Node + graph wiring

New `build_analytical_plan_node` (`agent/nodes.py`), wired
`classify_analytical_intent → build_analytical_plan → plan_query` (two
straight edges, no conditional routing, no new `END` branch — structurally
proven, see §3). Gated by a new shared `_planning_gate` helper
(`Settings.enable_query_planning` AND (`complexity_signals` OR
`intent_triggers_planning`) — the **exact same** condition
`plan_query_node` already used, now factored out so the two nodes can
never silently drift) **plus** `Settings.enable_analytical_planning`.

On success (LLM call succeeds, response parses, `validate_plan` finds
zero violations): stores `state["analytical_plan"]` (the validated dict)
and renders it into `state["query_plan"]` via `render_plan_as_steps`.
On **any** failure mode — gate didn't fire, feature flag off, Ollama
unreachable, unparseable response, or (the one genuinely new behavior)
**the plan failed deterministic validation** — `state["analytical_plan"]`
stays `None` and `state["analytical_plan_violations"]` records why (when
applicable), never a terminal status.

`plan_query_node` gained exactly one new early-return: when
`state["analytical_plan"]` is already a validated plan, it returns
`{"status": "generating"}` and does nothing else — a pure pass-through
that leaves the already-set `query_plan` alone, avoiding a second,
redundant free-text planning LLM call for the same question. Every other
line of `plan_query_node`'s own logic is **byte-identical** to its
pre-Prompt-12 shape.

### Config

`Settings.enable_analytical_planning: bool = True`,
`Settings.analytical_plan_max_tokens: int = Field(default=400, gt=0)` —
same on-by-default-but-escapable posture and naming convention as
`enable_query_planning`/`query_plan_max_tokens` immediately next to them.

## 3. Proof: this preserves the direct Text-to-SQL fallback by construction

- **Three-tier fallback, verified directly, not just claimed**:
  `test_validated_plan_is_stored_and_rendered_into_query_plan` (tier 1
  succeeds), `test_plan_that_fails_deterministic_validation_falls_back_to_none`
  (tier 1 is discarded in full on a violation — no partial trust),
  `test_invalid_plan_falls_back_and_plan_query_node_then_runs_normally`
  (tier 1's output, fed into `plan_query_node`, produces tier 2's
  unmodified free-text plan), and the pre-existing
  `TestPlanQueryNode` suite (unchanged — proving tier 2 is byte-identical
  to its pre-Prompt-12 self) together prove the full chain.
- **Never short-circuits the graph**: `test_build_analytical_plan_has_no_conditional_routing`
  (`tests/test_sql_agent_integration.py`) confirms `build_analytical_plan`
  has exactly one outgoing edge (`conditional=False`) to `plan_query` —
  the identical structural proof Prompt 11 established for
  `classify_analytical_intent`.
- **Cannot bypass authorization or the retry budget**: `Permission.ASK`
  is enforced before `run_agent`/the graph are even built (unchanged from
  every prior prompt's own proof); `build_analytical_plan_node` never
  reads/writes `max_retries` and its only authorization-adjacent read
  (`caller_roles`, for the restricted-column check) is passed straight
  through to the exact same `agent.authz.has_role_permission` function
  `validate_sql_node` already calls — no new authorization logic exists.
- **The real security gate is untouched**: `agent.sql_validator
  .validate_sql`'s safety-violation check and `validate_sql_node`'s own
  restricted-column gate are unmodified by this prompt and still run on
  every generated SQL statement regardless of whether a plan was ever
  built, validated, or rejected — `agent.plan_validator.validate_plan`
  is strictly an earlier, additional, non-load-bearing-for-security
  layer (master-contract rule 5).

## 4. Explicitly out of scope

- **Relationship-path checking is bounded to the already-retrieved
  schema's own declared FK edges** — not a full cross-schema graph
  search, and not `db/relationship_inference.py`'s own inferred (lower-
  confidence) candidates. A plan needing a real join path through a
  table that didn't make it into the top-k retrieved set is rejected
  (`no_relationship_path`), even if such a path exists in the full
  schema — a disclosed, bounded scope matching this codebase's existing
  "the retrieved schema is generation's own sandbox" posture.
- **`_OPERATION_SUPPORT` is a small, six-tag table, not a general SQL-
  dialect-feature catalog** — an unrecognized tag is never checked at
  all (fail open on ignorance). Expanding this table is a natural, small
  follow-up as more dialect-specific gaps are found in practice (the
  exact posture `db/adapter.py`'s own `DatabaseCapabilities` already
  takes for connection-level facts).
- **No live-LLM-verified new benchmark cases** — same disclosed reason
  Prompt 11 gave (`11_ANALYTICAL_INTENT_CONTRACT.md` §4): this
  development environment's live database connection doesn't expose the
  base tables a genuine new TREND/COMPARISON/RANKING benchmark case would
  need. Coverage here is contract-level (pytest) only, directly
  exercising every violation category and every named question shape
  (trend/comparison/ranking/segmentation/KPI-lookup) at the unit level.
- **No per-role restriction on who can trigger planning** — identical
  posture to every other LLM call this codebase already makes;
  `Permission.ASK` is the only gate, same as generation itself.
- **`docs/ARCHITECTURE.md` was not updated in this pass** — it was
  already stale relative to Prompt 11 (still describing "twelve nodes"
  despite that prompt's own 13th node) before this prompt touched
  anything; bringing it current is a real, disclosed, pre-existing gap
  this prompt did not introduce and chose not to silently paper over
  with a partial edit.

## 5. Testing

5 test files touched (3 new, 2 extended), 61 new tests:

- `tests/test_analytical_plan.py` (new, 12) — model defaults/frozen-ness/
  round-trip for every sub-model, `render_plan_as_steps` for an empty /
  metric-only / governed-metric / lookup / fully-populated plan.
- `tests/test_plan_validator.py` (new, 23) — one test per violation
  category (unknown table, unknown column, no metrics, incomplete
  metric, unknown governed-metric key, restricted column without/with
  permission, no relationship path, a path through an intermediate FK
  table, infeasible time range, unsupported operation per `db_type`,
  unrecognized operation tag never blocks), plus a fully-valid plan for
  each of TREND/COMPARISON/RANKING/SEGMENTATION/KPI-lookup (the prompt's
  explicit testing requirement) and the governed-metric-only pass case.
- `tests/test_llm_client_analytical_plan.py` (new, 16) — the parse
  fail-open contract (valid/fenced JSON, malformed JSON, JSON array
  instead of object, unrecognized aggregation value, shape-only
  parsing), prompt-builder DATA-framing/context-reuse, the rendered
  block's framing, and `generate_analytical_plan_from_llm`'s mocked call
  shape/`OllamaUnavailableError` propagation/token-budget wiring.
- `tests/test_agent_nodes.py` (+9) — `TestBuildAnalyticalPlanNode`
  (gate-skip on no signals, gate-skip on disabled flag, a validated plan
  stored and rendered into `query_plan`, a plan that fails validation
  falling back to `None` with violations recorded, the explicit
  fallback-to-`plan_query_node` end-to-end regression test, fail-open on
  unreachable Ollama, fail-open on an unparseable response, intent-alone
  triggering the gate) plus one new `TestPlanQueryNode` case (skips its
  own LLM call when a validated `analytical_plan` already exists, leaving
  the already-set `query_plan` untouched).
- `tests/test_sql_agent_integration.py` (+1 new, +2 fixed) — the new
  `test_build_analytical_plan_has_no_conditional_routing` structural
  proof, plus the two pre-existing tests
  (`test_retrieve_business_context_is_wired_between_golden_examples_and_plan_query`,
  `test_classify_analytical_intent_has_no_conditional_routing`) updated
  for the new node sitting between `classify_analytical_intent` and
  `plan_query`.

**Full suite: 2938 tests pass, zero regressions** (`pytest -q`, re-run
after every change in this prompt — up from the 2877 Prompt 11 reported).
`ruff check`/`black --check`/`mypy`/`bandit` clean on every new/touched
file.

## 6. Security / tenant-isolation / performance review

- **Security**: `agent.plan_validator.validate_plan` is a pure,
  deterministic function with no I/O and no LLM call of its own — it
  cannot itself be prompt-injected. The planning *generation* call
  (`generate_analytical_plan_from_llm`) carries the identical "DATA, not
  instructions" framing every other system prompt in `agent/llm_client.py`
  already requires. A rejected plan is discarded in full, never partially
  trusted (no "use the valid parts" salvage logic exists) — this is what
  keeps the invariant "an invalid structured plan degrades to the
  pre-Prompt-12 pipeline" true without a special case. No new user-
  controllable field reaches `agent.sql_validator`/execution directly;
  the plan only ever influences which *generation* prompt is built, and
  the real SQL-safety gate downstream is completely unaffected.
- **Tenant isolation**: No new tenant-scoped data is read or written —
  `build_analytical_plan_node` reads only `question`, `schema_context_text`,
  `schema_tables`, `retrieved_context`, `analytical_intent`,
  `governing_metrics`, `caller_roles`, and `selected_database`/`selected_model`,
  all already resolved/scoped by earlier nodes in the same run. No new
  database query of any kind (the FK-path check parses DDL text already
  in memory; it never queries live metadata).
- **Performance**: One additional LLM call per question, only when the
  pre-existing `plan_query_node` gate would already have fired (never on
  a plain LOOKUP/AGGREGATION question with no complexity signal) —
  deliberately lean (`analytical_plan_max_tokens=400` by default, no
  change to the schema context size, since it's the same `schema_context`
  `generate_sql` already sees). `validate_plan` itself is pure in-memory
  computation over an already-retrieved, small (`SCHEMA_TOP_K`-bounded)
  table set — negligible compared to any LLM call. Disabling
  `Settings.enable_analytical_planning` removes this cost entirely with
  zero other behavior change; `plan_query_node`'s own unmodified cost
  profile is the floor this can never exceed, since the two nodes never
  both make an LLM call for the same question.

## 7. Known limitations / remaining risks

- **An LLM-judged plan can itself be systematically wrong in a way
  `validate_plan` can't catch** — e.g. a plan that names the *wrong but
  real* column (one that exists, passes every structural check, and
  still produces a semantically incorrect answer). `validate_plan`
  verifies *existence, authorization, joinability, and capability
  support* — it has no opinion on whether a plan is the *right* one for
  the question, which remains `review_sql_node`'s (LLM-judged, advisory)
  and the user's own job, same as before this prompt.
- **No live-LLM run of the new unit-level test scenarios against a real
  Ollama server in this session** — `tests/test_plan_validator.py`
  verifies `validate_plan`'s own logic directly against hand-built plans;
  confirming a real local model actually produces plans this validator
  accepts at a useful rate is a natural follow-up (`scripts/run_benchmark.py`
  against a tagged set of cases, mirroring Prompt 11's own disclosed
  follow-up).
- **`_OPERATION_SUPPORT`'s six tags are a starting set, not exhaustive**
  — see §4.
- **`docs/ARCHITECTURE.md` remains stale** (pre-dates even Prompt 11) —
  see §4's disclosed decision not to partially patch it in this pass.

## Recommended next prompt

Per `02_TARGET_ARCHITECTURE.md` §5's own table: wire `analytics/`
(`ResultSummaryAnalyticsProvider`) into `generate_insight_node` and
surface `AnalyticsResult` in `AskResponse` — the same carried-forward
recommendation Prompts 04-11 have each named, still pending explicit
sign-off since it changes a live response shape. Alternatively, a
dedicated `docs/ARCHITECTURE.md` refresh pass (closing the gap §4/§7
above disclose, now two prompts deep) before it drifts further, or a
live-LLM run of `scripts/run_benchmark.py` to empirically measure how
often a real local model's structured plans actually clear
`validate_plan` (this prompt's own natural empirical follow-up, mirroring
Prompt 11's identical one for intent classification).
