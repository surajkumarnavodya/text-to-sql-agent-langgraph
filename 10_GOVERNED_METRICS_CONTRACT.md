# 10 — Governed Metrics, Dimensions & Semantic Contracts

Prompt 10 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`09_SEMANTIC_CATALOG_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/TEST/
REVIEW/FIX/DOCUMENT** — five new fields on the existing semantic catalog
(no new table), a new conflict-detection repository function, a new
LangGraph node + routing function enforcing governed-metric precedence,
a new mandatory-use prompt block, and additive API/config/eval surface.
No existing route, node, chunk type, or `Settings` field changed behavior
for a caller that doesn't use the new capabilities.

## 1. Inspection: what already existed vs. the real gaps

Prompt 09 built the entire draft→reviewed→published→superseded catalog
machinery, RBAC/ABAC, and retrieval integration (`ChunkType
.BUSINESS_CONCEPT`, advisory-only in both the generation and planning
prompts). What it explicitly left for later: `CatalogEntrySnapshot` had
**no** `approved_expression`/`source_tables` (distinct from `keys`)/
`filters`/`dimensions`/`aggregation` — `metric_definition_from_snapshot`
hard-coded these empty because nothing real existed to put there.
`review_sql_node` + `review_sql_against_plan_from_llm`
(`agent/nodes.py:883-963`, `agent/llm_client.py:1056-1110`) was confirmed
as the exact template for "check generated SQL against an authoritative
reference and retry if it doesn't match" — narrow PASS/FAIL LLM
judgment, fail-open on Ollama unavailability, sharing the existing
`retry_count`/`max_retries`/`AttemptRecord`/`error_history`/
`last_error_category` machinery every other retryable node failure
already uses — but it's gated on `query_plan` (itself gated on
`agent.complexity` signals), which a simple KPI question like "what's
our revenue?" never trips. Every existing retrieved-context prompt block
(`_build_business_context_block`) is framed purely advisory — "DATA,
reference material only, never authoritative" — with no precedent for
"you must use this." No duplicate/conflicting-definition detection
existed anywhere (`next_version_for_concept_key` only handles the *same*
`concept_key`'s own version history, never a *different* `concept_key`
claiming the same name). `eval/`'s `BenchmarkCase`/evaluators have no
cross-case equivalence notion at all.

## 2. What was built

### Governed-metric fields (extend, not duplicate, the Prompt-09 model)

`semantic/catalog.py::CatalogEntrySnapshot` gained `approved_expression:
str | None`, `source_tables: tuple[str, ...]`, `filters: tuple[str,
...]`, `dimensions: tuple[str, ...]`, `aggregation: str | None` —
mirrored on `identity.models.SemanticCatalogEntry` as 5 new nullable/
JSON columns (migration `e8f1c3a6d9b2`, chained after Prompt 09's
`d5e9f2a7b4c1`), threaded through `identity/repositories
/semantic_catalog.py::create_entry`/`entry_to_snapshot`, and
`api/semantic_catalog_schemas.py`'s request/response models (additive —
backward compatible). `metric_definition_from_snapshot` now maps
`formula=approved_expression`, `aggregation`, `valid_filters=filters`,
`source_tables` from these **real** fields instead of Prompt 09's
hard-coded empties — closing that prompt's own disclosed gap.
`retrieval/chunking.py::business_concept_chunk_from_catalog_entry`
renders all five into both the chunk's `text` and `extra` when present —
zero change in behavior for a non-metric entry with none of them set.

### Confirmed definitions take precedence — the acceptance-criterion mechanism

**Extraction, reusing retrieval already done, never a second query:**
`retrieval/retriever.py::extract_governing_metrics(items)` filters
already-retrieved chunks down to `BUSINESS_CONCEPT`-typed, `METRIC`-
`concept_type`-tagged entries (every one of which is `PUBLISHED` by
construction — see that chunk builder's own docstring). `agent.nodes
.retrieve_business_context_node` calls it once and sets the new
`AgentState["governing_metrics"]` alongside the existing
`retrieved_context`.

**A mandatory-use prompt block, not another advisory hint:**
`agent/llm_client.py::_build_mandatory_metrics_block` extends
`_build_plan_block`'s existing imperative-but-injection-safe framing
("you MUST use exactly this approved expression... never invent your
own aggregation... for a metric named here") — still explicitly DATA,
never instructions, the same standing security posture every prompt
block in this codebase carries. Wired into `_build_user_prompt` as the
*last* context section before `Question:`, for maximum attention.
`generate_sql_from_llm` gained a `governing_metrics` parameter, threaded
from `generate_sql_node`'s `state["governing_metrics"]` — present on
every retry automatically (unchanged existing persistence behavior).

**Enforcement, not just a stronger hint:** new `review_metric_
conformance_node`, inserted into the graph as `review_sql →
review_metric_conformance → validate_sql` (one node insertion; every
other edge unchanged — verified via `build_graph().get_graph().nodes`).
Skips entirely — zero cost, identical shape to every other pass-through
gate in this graph — when `state["governing_metrics"]` is empty or
`Settings.enable_metric_conformance_review` (new flag, default `True`)
is off. Otherwise calls new `review_sql_against_metrics_from_llm`
(clones `review_sql_against_plan_from_llm`'s exact PASS/FAIL contract
and fail-open behavior). A FAIL shares `review_sql_node`'s identical
retry-budget machinery (`outcome="metric_definition_not_used"`, a new
`_ERROR_CATEGORY_HINTS` entry, same `retry_count`/`max_retries` pool —
never a second, separate loop).

**Deliberately not a `sql_validator.py` safety check** — an LLM-judged
accuracy aid (two SQL expressions can be computationally equivalent
without being AST-identical), kept structurally separate from the
deterministic, fail-closed security validator, per master rule 5.

### Conflict detection

`identity/repositories/semantic_catalog.py::find_conflicting_published_
entries` scans `PUBLISHED` rows in the same `(tenant_id, database_id,
concept_type)` scope for a **different** `concept_key` whose
`business_name`/`synonyms` case-insensitively overlap — a plain
Python-side set comparison over an already-small, already-fetched row
set (no cross-dialect JSON-containment SQL needed). `api/semantic_
catalog.py`'s new `_check_conflicts` helper runs it at both `create` and
`publish` time, non-blocking (the entry is still created/published
either way — this codebase's standing "surface ambiguity, never
auto-resolve it" posture), surfaced via new `CatalogEntryOut
.conflicting_entry_ids`/`conflicting_entry_names` fields, and logged via
the existing `security.audit_log.log_security_event` structured-
observability hook when non-empty.

### Testing proof of the acceptance criterion

`tests/test_llm_client_metric_conformance.py`'s
`test_two_different_phrasings_that_retrieve_the_same_metric_produce_
identical_blocks` and `test_two_differently_worded_questions_produce_
identical_mandatory_block_text` are the direct, deterministic proof:
the mandatory block (and therefore the SQL-generation guidance) is a
pure function of the governing-metric data, never of the question's own
phrasing — verified both at the block-builder level and through the
real `_build_user_prompt` assembly path. A manual end-to-end smoke test
(create → review → publish → two different NL questions both resolving
to the same chunk → identical mandatory block) confirmed this live.

## 3. Explicitly out of scope

- **No cross-case equivalence evaluator in `eval/`** — `eval/schema.py`
  gained a `"metric_consistency"` category and `eval/benchmark/
  real_world.yaml` gained two new cases (`metric_consistency_aov_
  phrasing_one`/`_two`) pairing differently-worded AOV questions against
  the *identical* `expected_sql`, but each is still scored individually
  against its own `expected_sql` — this framework has no mechanism to
  compare two different cases' results to each other, and building one
  was out of scope for this prompt. The real proof of the acceptance
  criterion is the deterministic mandatory-block test above, not this
  benchmark category.
- **No AST-level structural verification of "approved expression used."**
  `review_metric_conformance_node`'s check is an LLM judgment, not a
  `sqlglot`-based structural comparison — deliberately, since two SQL
  expressions can be computationally equivalent without matching
  syntactically (a different column alias, join order, or CTE shape).
- **No automatic promotion of an inferred metric** (e.g. from a future
  onboarding-engine integration) into a governed catalog entry — an SME
  or operator still creates/reviews/publishes every entry explicitly.
- **`dimensions` is a flat list of allowed breakdown-dimension names**,
  not a validated reference to real `DIMENSION`-concept-type catalog
  entries or live schema columns — a disclosed, narrower scope than a
  full semantic-layer dimension model.
- **Conflict detection is name/synonym overlap only**, not a semantic
  (embedding-similarity) check — two metrics describing the same
  underlying concept with completely different names/synonyms are not
  detected as conflicting.

## 4. Testing

9 test files touched (2 new, 7 extended), 50 new tests, all passing:

- `tests/test_semantic_catalog_model.py` (+4) — the new snapshot fields'
  defaults/round-trip, and `metric_definition_from_snapshot`'s corrected,
  real field mapping (including the regression case: a metric with no
  `approved_expression` set never falls back to `technical_name`).
- `tests/test_identity_repositories_semantic_catalog.py` (+7) — the new
  fields' round-trip through `create_entry`/`entry_to_snapshot`, and a
  full `TestFindConflictingPublishedEntries` class (no-conflict, synonym
  collision, case-insensitive business-name collision, self-exclusion,
  draft/reviewed entries never counted, tenant/database/concept-type
  scoping).
- `tests/test_retrieval_catalog_chunking.py` (+3) — the new fields
  rendered into both `text` and `extra`; a non-metric entry renders none
  of them.
- `tests/test_retrieval_governing_metrics.py` (new, 7) —
  `extract_governing_metrics`'s type-filtering (metric vs. non-metric vs.
  non-business-concept chunks), and the direct determinism proof (two
  different similarity scores for the same chunk produce identical
  extracted dicts).
- `tests/test_llm_client_metric_conformance.py` (new, 12) — the
  mandatory-block builder's rendering/framing/determinism,
  `_build_user_prompt`'s wiring and placement, and `review_sql_against_
  metrics_from_llm`'s PASS/FAIL/unparseable-fail-open/unreachable-Ollama
  contract (mocked Ollama client, no real server needed).
- `tests/test_agent_nodes.py` (+9, +1 fixed) — `TestReviewMetricConformanceNode`
  mirroring `TestReviewSqlNode`'s full coverage shape (skip on empty/
  disabled, pass, fail-retry, fail-exhausted, fail-open on Ollama,
  `max_retries` override), plus the pre-existing `test_route_after_review`
  parametrization corrected for the new routing target
  (`review_metric_conformance`, not `validate_sql` directly) and a new
  `test_route_after_metric_conformance`.
- `tests/test_api_semantic_catalog.py` (+8) — governed-field round-trip
  through create/get, the published chunk actually carrying
  `approved_expression`/`aggregation` in a fake vector store, and a full
  `TestConflictDetection` class (no-conflict, detected-on-create,
  never-blocks-creation, detected-on-publish, cross-tenant entries never
  conflict).

**Full suite: 2775 tests pass, zero regressions** (`pytest -q`, re-run
after every change in this prompt). `ruff check`/`black --check`/`mypy`
clean on every new/touched file.

## 5. Security / tenant-isolation / performance review

- **Security**: The mandatory-metrics prompt block carries the identical
  "DATA, never instructions" framing every other block in this codebase
  already requires — a governed metric's `business_name`/`approved_
  expression` text (operator-authored, already passed through SME
  review before publish) is rendered the same injection-safe way
  `_build_plan_block` already renders an LLM-authored plan. The
  conformance check is explicitly *not* a security control (master rule
  5) — a FAIL only ever triggers a retry, never bypasses `agent
  .sql_validator`'s own deterministic, fail-closed checks, which still
  run unconditionally afterward. `log_security_event` for a detected
  conflict never includes raw user input beyond what's already stored
  (business names/concept keys), consistent with this codebase's
  existing redaction posture.
- **Tenant isolation**: `find_conflicting_published_entries` is scoped
  by `(tenant_id, database_id, concept_type)` exactly like every other
  catalog query — verified directly (`test_scoped_to_tenant_database_
  and_concept_type`) and via a real HTTP test confirming a cross-tenant
  entry is never reported as a conflict. No new tenant-boundary surface
  was introduced; the new fields/conflict check ride entirely on
  Prompt 09's existing tenant-scoped `SemanticCatalogEntry` rows.
- **Performance**: `extract_governing_metrics` is a pure in-memory
  filter over chunks `retrieve_business_context` already fetched — zero
  additional query cost. `review_metric_conformance_node` costs exactly
  one additional LLM call, and only for a question that actually
  retrieved a governing metric (the common non-KPI question pays
  nothing). `find_conflicting_published_entries` is one indexed query
  plus an in-memory scan bounded by "how many published entries share
  this tenant/database/concept_type" — not expected to be large in
  practice, and disclosed as such rather than optimized prematurely.

## 6. Known limitations / remaining risks

- **An LLM-judged conformance check can itself be wrong** — both a false
  pass (the reviewer model misjudges two different formulas as
  equivalent) and a false fail (flagging a genuinely equivalent
  rewrite) are possible, same inherent limitation `review_sql_node`'s
  plan-conformance check already has and discloses. The deterministic
  security validator downstream is the actual safety net; this is an
  accuracy aid.
- **No retrieval-level tenant filtering** — a published catalog entry's
  chunk carries no `tenant_id` field at all (unchanged from Prompt 09);
  in this app's current, deliberately single-tenant-platform-wide
  posture (`security/tenancy.py`) this is not a real leak today (every
  real user resolves to the same tenant), but would need addressing
  before a genuine multi-tenant retrofit, same disclosed boundary
  Prompt 09 already names.
- **Conflict detection has no "resolve" workflow** — a detected conflict
  is surfaced, never auto-merged or flagged for a required review step;
  an operator must notice and act on it manually (e.g. by superseding
  one of the two entries, or editing one's synonyms to stop colliding).
- **The two new eval cases don't exercise a live LLM against a real
  governed catalog entry** — they test the benchmark harness's own
  loading/shape, not an actual live run proving the agent consistently
  resolves both phrasings the same way end-to-end (that would require
  `scripts/run_benchmark.py` against a real Ollama + a real published
  catalog entry, outside this prompt's automated-test scope, same manual/
  real-DB-required posture every other benchmark run already has).

## Recommended next prompt

Per `02_TARGET_ARCHITECTURE.md` §5's own table: wire `analytics/`
(`ResultSummaryAnalyticsProvider`) into `generate_insight_node` and
surface `AnalyticsResult` in `AskResponse` — unchanged recommendation
carried forward from Prompts 04-09, still pending explicit sign-off
since it changes a live response shape. Alternatively, a small backfill
script closing Prompt 09's own disclosed vector-store-sync-failure gap,
or a live-LLM run of the two new `metric_consistency` benchmark cases
against a real published AOV catalog entry to empirically confirm this
prompt's acceptance criterion end-to-end (not just the deterministic
unit-level proof already in place).
