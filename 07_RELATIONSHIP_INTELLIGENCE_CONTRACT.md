# 07 — Relationship & Schema Intelligence

Prompt 07 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`06_DATABASE_DISCOVERY_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/TEST/
REVIEW/FIX/DOCUMENT** — one new module (`db/relationship_inference.py`),
a small, catalog-only extension to `db/schema_introspection.py`, a new
chunk builder in `retrieval/chunking.py`, additive wiring in
`retrieval/ingestion.py`, and a real, previously-missing plumbing fix in
`agent/llm_client.py`/`agent/nodes.py` (planning never saw relationship
context at all). No existing route, node, or `Settings` field changed
behavior for a caller that doesn't use the new capabilities.

## 1. Inspection: what already existed vs. the real gaps

`db/schema_introspection.py` already discovered real, declared foreign
keys (`ForeignKeyInfo`) — a `DATABASE_FACT`, no inference needed.
`retrieval/chunking.py::relationship_chunks_from_schema` already turned
every declared FK into a dedicated, independently-retrievable
`relationship`-type chunk. `agent.provenance.DataTruthLevel` (built in
Prompt 02) already defined the `DATABASE_FACT`/`AI_INFERENCE`/
`CONFIRMED_BUSINESS_TRUTH` vocabulary this prompt needed — built ahead
of any consumer, per that module's own docstring, and never consumed by
anything until now. None of this needed rebuilding.

Real gaps, found by reading the actual code (not assumed):

1. **No unique-constraint/index discovery** — needed to know whether a
   column is even a *legal* FK target (a real FK's target is always a
   primary key or a unique constraint).
2. **No relationship inference at all** — only a real, declared FK ever
   produced a relationship chunk; a missing one never did, regardless of
   how obvious the naming convention.
3. **No confidence/evidence vocabulary for an unconfirmed relationship.**
4. **Planning never saw relationship context, confirmed or inferred** —
   confirmed by reading `generate_query_plan_from_llm`'s actual
   signature: `question` + `schema_context` only. The prompt's own
   "feed approved relationships to planning" phrasing named a real,
   verifiable gap, not a rhetorical one.

## 2. What was built

### `db/schema_introspection.py`

`TableSchemaInfo.unique_constraints` — each entry one unique
constraint's column names, from `Inspector.get_unique_constraints`
(catalog-only, same cost class as the existing PK/FK calls, skipped for
views exactly like PK/FK already are).

### `db/relationship_inference.py` (new)

- **`infer_relationships(tables, min_confidence=0.6)`** — structural
  inference, zero queries. A candidate needs, all three, or it's
  rejected outright (not scored lower):
  - **Name similarity**: an exact column-name match to the target
    column, or the column follows the `{target_table_core}+Key/Id/Code/
    No/Num/Number` convention (warehouse-style prefixes like `Dim`/
    `Fact` stripped first). No name signal at all → no candidate,
    regardless of type/uniqueness — this is what keeps a
    type-compatible-but-unrelated column ("misleading names") from ever
    being proposed.
  - **Type compatibility**: both columns' SQLAlchemy-rendered type
    strings fall into the same coarse family (integer/text/decimal/
    datetime/boolean/uuid). A mismatch is a hard rejection.
  - **Target uniqueness**: the target column is the target table's PK or
    a single-column unique constraint. Composite (multi-column) targets
    are out of scope for this increment (§3).
  - Classifies direction: `one_to_one` if the source column is itself
    unique in its own table, `many_to_one` otherwise.
  - Every result: `truth_level=AI_INFERENCE`, itemized `evidence`
    (signal name, 0..1 score, human-readable detail), `confidence` a
    deterministic function of the name-match strength (the two hard
    gates already passed just to reach scoring contribute a fixed
    floor).
- **`verify_candidates_with_data(candidates, engine, schema, sample_size)`**
  — explicitly opt-in, bounded refinement: null fraction of the source
  column, and what fraction of a bounded sample of non-NULL source
  values are found in the target column. Bounded via `fetchmany()`
  alone (no `LIMIT`/`TOP` text needed) — the exact same established
  pattern `db/value_sampling.py::_sample_column` already uses, not a
  second implementation of "bounded sampling." Fails open per-candidate
  on any query error (the structural result is kept, unrefined, not
  dropped).

### `retrieval/chunking.py`

`inferred_relationship_chunks_from_schema` — one more
`ChunkType.RELATIONSHIP` chunk per candidate, deliberately the *same*
type a real FK's chunk uses (both retrieved/scored/labeled the same
way). What keeps rule 10 true is the chunk's own **text**, not a
separate type or an `extra` field the LLM never sees (`retrieval.models
.Chunk`'s own docstring: extra is never rendered into the prompt): every
inferred chunk opens with `"CANDIDATE relationship (NOT a declared
foreign key -- inferred, not confirmed; confidence 0.XX): ..."`.
`object_name` is prefixed `candidate:` so its chunk ID structurally
cannot collide with a real relationship's.

### `retrieval/ingestion.py`

`build_schema_chunks` gained `settings`/`relationship_candidates`
(both keyword-only, both optional, defaulting to the exact structural-
only, engine-free behavior that keeps this function testable without a
connection — unchanged for every existing caller). `run_ingestion`
computes structural candidates whenever it has a live engine (i.e.
never when a caller passes `tables` in directly) and additionally runs
the data-verification pass when `Settings
.enable_relationship_data_verification` is on, passing the richer,
already-verified candidate list into `build_schema_chunks` instead of
letting it recompute a structural-only one and lose the refinement.

### `agent/llm_client.py` / `agent/nodes.py`

`_build_plan_relationship_block` renders only the `relationship`-type
entries of `AgentState["retrieved_context"]` (real and candidate alike,
each keeping its own chunk's framing) into `generate_query_plan_from_llm`'s
prompt — a parameter that didn't exist before this prompt.
`plan_query_node` threads `state["retrieved_context"]` through (already
populated by `retrieve_business_context_node`, which runs immediately
before `plan_query` in the graph — verified from `agent/graph.py`'s own
edge list, not assumed). Every other business-context chunk type
(glossary, metric, ...) is deliberately left out of the planning
prompt — still generation-only, unchanged.

## 3. Explicitly out of scope

- **Composite (multi-column) candidate FKs.** A real, disclosed
  limitation — matching two or more columns' worth of name/type/
  uniqueness signals in combination is a meaningfully harder problem
  than the single-column case and wasn't attempted here.
- **Any promotion path from candidate to confirmed.** No UI/API exists
  in this codebase for a human to mark an inferred relationship
  `CONFIRMED_BUSINESS_TRUTH` — "approved" relationships fed to planning
  today means real, declared FKs only; a candidate is fed to planning
  exactly as unconfirmed as it is to generation, never promoted along
  the way.
- **Indexes as a distinct discovery field.** Only unique constraints
  were added (the load-bearing fact for "is this a legal FK target") —
  a general index inventory (including non-unique indexes) is a
  separate, disclosed follow-up, not silently folded in here.
- **Cross-database relationship inference.** Each configured database's
  tables are inferred against only that same database's own tables —
  unchanged from every other per-database-scoped mechanism already
  documented (schema indexing, golden examples, business-context
  retrieval).

## 4. Testing

- `tests/test_schema_introspection.py` (+3): unique-constraint capture
  (single- and multi-column), and confirmation views never trigger
  unique-constraint reflection (mirrors the existing PK/FK view-skip
  tests).
- `tests/test_relationship_inference.py` (new, 18 tests): every scenario
  the prompt names by name — a missing FK correctly inferred, a
  naming-convention match scoring lower than an exact match, a column
  already part of a declared FK never re-proposed, misleading names
  rejected (type-compatible-but-unrelated name, and no name signal at
  all regardless of type), incompatible types rejected, one-to-one vs.
  many-to-one direction classification, many-to-many via a junction
  table (no direct candidate between the two outer tables, both
  junction-side candidates correctly `many_to_one`), views never a
  source or target, the `min_confidence` gate, and the full data-
  verification pass (null-heavy key lowering confidence, full/zero value
  overlap raising/lowering it, fail-open on a query error, confidence
  never exceeding 1.0).
- `tests/test_chunking.py` (+5): one chunk per candidate, the
  unmistakable "CANDIDATE relationship... inferred, not confirmed"
  text framing, `extra` carrying confidence/evidence/truth_level, no
  chunk-ID collision with a real relationship, join-condition/direction
  rendering.
- `tests/test_ingestion.py` (+7): `build_schema_chunks`'s default-on
  inference and its `enable_relationship_inference=False`/
  `relationship_candidates` override paths; `run_ingestion` only ever
  attempting data verification when it has a live engine, never for a
  caller that passed `tables` in directly, and never when the opt-in
  flag is off.
- `tests/test_llm_client_planning.py` (+9): the relationship-block
  renderer (None for no relationship chunks, filters out every other
  chunk type, preserves a candidate's own unconfirmed framing, "DATA/
  never instructions" framing) and the updated plan user-prompt
  builder (a true no-op with no relationship context, correct
  Schema→Relationship→Question ordering when present).
- `tests/test_agent_nodes.py`: existing `TestPlanQueryNode` test updated
  to assert `retrieved_context` is actually threaded through.

Full suite: **2519 tests pass (2479 pre-existing + 40 new), zero
regressions** (`pytest -q`, ~120-135s across runs). `ruff check` /
`black --check` / `mypy` on every touched/new file: clean.

## 5. Security / tenant-isolation / performance review

- **Security**: `db/relationship_inference.py` never executes against,
  and is never fed into, `agent.sql_validator`'s LLM-output gate — it's
  schema-analysis tooling that runs at ingestion time, consumed
  downstream only as a reference-only retrieval chunk, the same
  boundary every other business-context chunk already has. The
  data-verification pass's own SQL uses bind parameters for every
  data-derived value (the sampled source values in the `IN (...)`
  lookup) and the connected engine's own `identifier_preparer.quote`
  for every identifier — the same two mitigations `db/value_sampling.py`
  already established for exactly this class of risk (attacker-writable
  data, and a maliciously-named table/column), not a second,
  independently-reasoned-about implementation of either.
- **Tenant isolation**: inference runs per configured database, against
  only that database's own already-introspected tables — unchanged from
  every other per-database-scoped mechanism in this codebase.
- **Performance**: structural inference is O(tables × columns × tables)
  over already-in-memory metadata — no query, negligible cost even for a
  large schema. The data-verification pass is opt-in specifically
  because it isn't free (one to two bounded queries per candidate); off
  by default, so no existing ingestion run's cost changed.

## 6. Known limitations / remaining risks

- The structural heuristic can legitimately produce **more than one**
  plausible candidate for the same source column when two target tables
  share a naming convention (e.g. a column named identically to both a
  `Customers.Id` via convention-suffix match *and* a
  `CustomerProfile.CustomerId` via exact-name match) — verified live
  during development, not hypothetical. Both are surfaced, ranked by
  confidence; nothing silently picks one, which is the conservative,
  correct behavior for an unconfirmed inference, but it does mean a
  retrieval result can show two competing candidates for the same
  column.
- The data-verification pass's null-fraction/value-overlap sampling
  reads the *first* N rows the engine happens to return for an
  unordered `SELECT` — not a statistically random sample. Disclosed,
  not fixed: the same tradeoff `db/value_sampling.py` already accepts
  for the same reason (bounded, cheap, good enough for a confidence
  *signal*, not a rigorous statistic).
- No live database with a genuinely ambiguous or misleading real-world
  schema was used to re-verify end-to-end — verification is against
  real SQLAlchemy type objects and a mocked `Inspector`/engine layer
  (same level as this series' other prompts' own disclosed limitation).

## Recommended next prompt

Per `02_TARGET_ARCHITECTURE.md` §5's own table: wire `analytics/`
(`ResultSummaryAnalyticsProvider`) into `generate_insight_node` and
surface `AnalyticsResult` in `AskResponse` — unchanged recommendation
carried forward from Prompts 04-06, still pending explicit sign-off
since it changes a live response shape.
