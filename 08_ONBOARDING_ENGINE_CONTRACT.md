# 08 — Data Profiling, PII & Client Onboarding Engine

Prompt 08 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`07_RELATIONSHIP_INTELLIGENCE_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/
TEST/REVIEW/FIX/DOCUMENT** — a new `onboarding/` package (pure-logic
pipeline stages, each independently testable with plain objects/a real
SQLite database), a new `identity/` persistence slice (3 ORM tables + an
Alembic migration + a repository module, following `identity/
repositories/shares.py`'s own split exactly), a new RBAC permission pair,
and a new REST surface (`api/onboarding.py`). Scope, per the user's own
explicit choice when asked to calibrate: **"Full pipeline, real depth on
the new parts"** — discovery/relationship stages reuse Prompts 06/07
unchanged; every other stage (profiling, PII detection, semantic
inference/ambiguity detection, SME review, semantic contract, golden
questions, evaluation) is genuinely new, tested logic, not a stub.

## 1. Inspection: what already existed vs. the real gaps

`db/schema_introspection.py` (Prompt 06) already discovered tables/
columns/types/FKs/views/row-count estimates. `db/relationship_inference.py`
(Prompt 07) already inferred undeclared FK candidates with confidence/
evidence. `agent.provenance.DataTruthLevel` (Prompt 02) already defined
the DATABASE_FACT/AI_INFERENCE/CONFIRMED_BUSINESS_TRUTH vocabulary this
prompt needed. `config/sensitive_columns.py`/`config/table_descriptions.py`
already *enforced* a hand-authored PII/semantic classification once one
exists — but nothing in the codebase ever *produced* one; both files
assume a human already wrote the YAML. `identity/repositories/shares.py`
+ `identity/share_policy.py` already established the exact RBAC+ABAC+
background-job-free pattern this prompt's own job engine needed.
`db/value_sampling.py` already established the bounded-`fetchmany()`+
identifier-quoting sampling pattern every new profiling/verification
query reuses.

Real gaps, found by reading the actual code (not assumed):

1. **No profiling at all** — no null%/distinct/min/max/avg/percentile/
   distribution/duplicate-key/orphan-row computation existed anywhere.
2. **No PII detection** — `config/sensitive_columns.py` only *consumes* a
   classification; nothing produced one.
3. **No semantic-role inference** — `config/table_descriptions.py` only
   *consumes* column notes; nothing inferred a column's likely business
   role (identifier/measure/category/free_text/...) or flagged ambiguity.
4. **No SME review workflow, no job/state-machine persistence, no
   onboarding API** — this entire engine (the prompt's actual acceptance
   criterion) didn't exist.
5. **No golden-question generation or pre-publish smoke-test** — the only
   existing few-shot mechanism (`embeddings/golden_examples.py`, Prompt
   before this series) requires a human to already know good (question,
   SQL) pairs; nothing proposed candidates for a brand-new database.

## 2. What was built

### `onboarding/` (new package — pure logic, zero `identity/` dependency except `jobs.py`)

- **`pii_detection.py`** — `detect_pii_columns(tables)`: zero-query,
  compound column-name-pattern matching (email/phone/ssn/date_of_birth/
  address/ip_address/person_name/credit_card/national_id/gender), plus a
  low-confidence bare-"Name"-on-a-person-shaped-table heuristic. Every
  finding is `AI_INFERENCE`. `verify_pii_with_data(findings, engine, ...)`
  — opt-in (`Settings.enable_pii_data_verification`), bounded sample,
  refines confidence via an aggregate match-rate float; **never returns,
  logs, or stores a raw sampled value** (locked in by a dedicated test
  asserting a secret-looking sample string never appears in any evidence
  string).
- **`profiling.py`** — `profile_column(engine, table, column, type, ...)`:
  one exact-aggregate query (COUNT/COUNT DISTINCT/MIN/MAX/AVG) plus a
  bounded sample for numeric percentiles (Python-computed, not a
  dialect-specific `PERCENTILE_CONT`) and low-cardinality top-N value
  distribution. `find_duplicate_keys`/`find_orphan_rows` — real
  `GROUP BY ... HAVING COUNT(*) > 1` / bounded-sample `IN (...)` checks
  against every PK/unique-constrained column and every relationship
  candidate, respectively.
- **`semantic_inference.py`** — `infer_semantic_labels(tables,
  relationships, column_profiles)`: scores every plausible (label,
  evidence) candidate per column from key-ness/type/profiled cardinality,
  picks the best, and sets `is_ambiguous` whenever the best candidate
  isn't both ≥0.65 confident *and* ≥0.15 ahead of the runner-up — the
  literal mechanism behind the prompt's acceptance criterion.
- **`golden_questions.py`** — `generate_candidate_questions(...)`:
  **template-based, never LLM-generated** (master-contract rule 5),
  4 shapes (count/aggregate_by_category/top_n/join_count), each carrying
  a deterministically-generated `candidate_sql` alongside the NL text.
- **`evaluation.py`** — `evaluate_candidates(...)`: re-runs each
  candidate's SQL through the *exact* same two gates every other SQL in
  this codebase goes through (`agent.sql_validator.validate_sql`/
  `enforce_row_limit`, then `db.execution.execute_readonly_sql`) —
  explicitly not `eval/`'s execution-accuracy benchmark (no gold answer
  exists yet for a brand-new database).
- **`semantic_contract.py`** — `build_semantic_contract(confirmed_items)`:
  assembles only `"confirmed"`-decision items into the exact shape
  `config/table_descriptions.yaml`/`config/sensitive_columns.yaml`
  already define — an operator applies it manually (no runtime API to
  register a database, same disclosed boundary Prompt 06 already set).
- **`policy.py`** — `authorize_onboarding_action(...)`: deny-by-default
  RBAC (`ONBOARDING_MANAGE` for create/discover/publish/cancel/retry,
  `ONBOARDING_REVIEW` for deciding items, either for viewing) + ABAC
  (tenant match), mirroring `identity/share_policy.py` exactly.
- **`jobs.py`** — the only module importing both the pure-logic stages
  above *and* `identity/`'s persistence layer. `run_discovery_stage`
  chains discovery → relationships (Prompt 07, unchanged) → profiling →
  PII → semantic inference → golden questions, persisting every
  candidate as an `OnboardingReviewItem`. `publish_job` requires every
  item decided (no pending item may reach a published artifact — rule
  10's "never silently promote inference" enforced structurally, not by
  convention), then builds `semantic_contract`/`golden_questions`/
  `evaluation_report` artifacts. `cancel_job`/`retry_job` complete the
  state machine. **No background worker, by design** — see §6.

### `identity/` additions

- **`models.py`** — `OnboardingJob` (connection fields, **never a
  password field** — nothing here to accidentally persist one into),
  `OnboardingReviewItem` (one per PII/relationship/semantic-label/
  golden-question candidate, `decision` pending/confirmed/rejected),
  `OnboardingArtifact` (versioned, append-only — `add_artifact` always
  inserts a new row). Table count 19 → 22.
- **`migrations/versions/c4d8e1f6a9b3_onboarding_engine.py`** — chains
  after the conversation-sharing migration; creates all 3 tables +
  indexes.
- **`rbac.py`** — `Permission.ONBOARDING_MANAGE`/`ONBOARDING_REVIEW`,
  seeded, `ONBOARDING_REVIEW` added to `_ANALYST`, `ONBOARDING_MANAGE` to
  `_ADMIN`.
- **`repositories/onboarding.py`** — plain CRUD, zero authorization
  logic (mirrors `repositories/shares.py`'s split): `create_job`,
  `get_job_by_id`, `list_jobs_for_tenant`, `update_job_status`,
  `increment_retry_count`, `add_review_items`, `get_review_item_by_id`,
  `list_review_items`, `decide_review_item`, `add_artifact`,
  `list_artifacts`, `get_latest_artifact`.

### `api/onboarding.py` / `api/onboarding_schemas.py` (new)

10 routes under `/onboarding`: create/list/get job, discover, list/decide
review items, publish, cancel, retry, list artifacts. `db_password`
appears on exactly 3 request bodies, never a response model. Discover/
publish build a **throwaway** `Engine` directly (`create_engine(...)`,
disposed in `finally`) — deliberately bypassing `db.connection`'s
process-wide cached-engine pool, whose own docstring assumes "a handful"
of configured connections, an assumption this dynamic, many-distinct-
candidate-databases-over-time engine would violate.

### `config/settings.py`

`enable_pii_data_verification` (default `False`) + `pii_data_verification
_sample_size` — kept **separate** from Prompt 07's `enable_relationship
_data_verification` flag, per the prompt's own "minimize sensitive
sampling" requirement: an operator can opt into relationship value-
overlap sampling (never PII-shaped) without also opting into sampling
columns already name-flagged as likely PII.

## 3. Explicitly out of scope

- **No background worker / no stored connection secret.** A connection
  password is supplied fresh per API call, used immediately to build a
  throwaway engine, and discarded. There is deliberately no "resume this
  job automatically" path — a job stuck mid-stage because its own API
  call crashed is only recoverable via `retry_job`, which requires a
  fresh call with fresh credentials, exactly like the original call did.
  This is the deliberate, disclosed tradeoff for never persisting a
  password.
- **Discovery/relationship inference themselves are unchanged** —
  Prompts 06/07 own that logic; `onboarding/jobs.py` calls
  `introspect_schema`/`infer_relationships` directly, never reimplements.
- **No runtime API applying a published semantic contract** to
  `config/table_descriptions.yaml`/`config/sensitive_columns.yaml` — an
  `OnboardingArtifact` is a reviewable draft an operator applies
  manually, the same disclosed boundary Prompt 06 already set for
  registering a new database at all.
- **No per-role model restriction, no live-reload of RBAC mid-process**
  beyond what `identity/rbac.py`'s existing DB-backed design already
  provides.
- **`config/table_descriptions.yaml`/`config/sensitive_columns.yaml`
  remain keyed by bare table name, not `(database, table)`** — unchanged,
  pre-existing limitation (documented in `CLAUDE.md`'s multi-database
  section), not addressed by this prompt.

## 4. Testing

9 new test files, 116 tests, all passing:

- `tests/test_onboarding_pii_detection.py` (16) — compound-pattern
  matching, false-positive avoidance (`ProductName`/`CompanyName` never
  flagged), the bare-name person-table heuristic, sorting, and the
  data-verification pass's confidence refinement + the hard "no raw
  value ever leaves this module" guarantee.
- `tests/test_onboarding_profiling.py` (12) — real in-memory SQLite
  (`StaticPool`): numeric aggregates/percentiles, categorical top-values,
  fail-open on a query error, duplicate-key detection (true negative +
  true positive + view-skip), orphan-row detection (true positive + zero
  orphans + no-non-null-source + query error).
- `tests/test_onboarding_semantic_inference.py` (12) — identifier/
  foreign_key/flag/timestamp/measure/category/free_text labeling from
  type+profile signals, and the ambiguity margin/floor (a measure-vs-
  category near-tie correctly flagged `is_ambiguous=True`).
- `tests/test_onboarding_golden_questions.py` (9) — all 4 question
  shapes' exact SQL text, confidence dampening for an ambiguous measure/
  category, the cross-product for multiple measures×categories, and the
  `max_questions` cap preserving earlier (simpler, more reliable) shapes
  first.
- `tests/test_onboarding_semantic_contract.py` (9) — only `"confirmed"`
  items ever contribute; a PII-only table gets a `sensitive_columns`
  entry but no `table_descriptions` entry; relationship/semantic-label
  items correctly populate `key_relationships`/`column_notes`; sorting.
- `tests/test_onboarding_evaluation.py` (5) — real SQLite: a good query
  passes with a correct row count, a malicious `DROP TABLE` candidate is
  rejected by validation (never reaches the database — verified the
  table still exists afterward), a nonexistent-table query fails
  execution cleanly, one bad candidate never aborts evaluating the rest.
- `tests/test_onboarding_policy.py` (13) — every RBAC/ABAC branch,
  including cross-tenant denial taking priority over a fully-permissioned
  admin's own RBAC check.
- `tests/test_identity_repositories_onboarding.py` (18) — real in-memory
  SQLite: job creation never accepts a password parameter (a structural,
  signature-level check, not just a runtime one), status transitions
  clearing a stale error, review-item bulk-insert + decision filtering,
  artifact versioning (per-type, never overwritten).
- `tests/test_onboarding_jobs.py` (12) — two real SQLite databases
  (identity + a "target" database with real FK-shaped data): full
  discovery success populating all 4 review-item types, an injected
  failure correctly marking the job `"failed"` with a redacted message,
  full publish success producing exactly 3 artifacts with a 100% smoke-
  test pass rate, publishing with a pending item raising and marking the
  job failed, cancel from a non-terminal status + rejecting cancel on an
  already-terminal one, retry resuming to the correct stage for a
  discovery vs. a publish failure.
- `tests/test_api_onboarding.py` (10) — full HTTP surface via
  `TestClient`: RBAC (admin can create, plain user/analyst-alone
  forbidden), tenant isolation (a cross-tenant job lookup is a 404, not a
  403 — anti-enumeration), the full create→discover→review→publish
  lifecycle end-to-end with a real analyst/admin role split, publish
  rejected with a pending item (409), cancel + double-cancel (409),
  retry resetting a genuinely failed discovery back to `"pending"`.

**Full suite: 2635 tests pass (2519 pre-existing + 116 new), zero
regressions** (`pytest -q`, ~98-125s across runs). `ruff check` /
`black --check` / `mypy` on every touched/new production file: clean
(two pre-existing, unrelated findings in `api/main.py` confirmed via
`git stash` to predate this prompt's changes — left alone, not claimed as
fixed, per `CLAUDE.md`'s own documented CI-hygiene-gap tracking).

## 5. Security / tenant-isolation / performance review

- **Security**: No connection secret is ever persisted — `OnboardingJob`
  has no password field at all (not just a validated-empty one), and
  `create_job`'s own signature has no parameter to accidentally pass one
  into (locked in by a dedicated test inspecting the signature directly).
  Every raw driver/exception string is redacted (`security.redaction
  .redact_secrets`) before being stored in `error_message` or an
  evaluation result. A review item's own `payload` never embeds a raw
  sampled PII value — `PiiEvidence.detail` is built exclusively from
  aggregate rates/pattern names, verified by a dedicated test. Every
  evaluated candidate SQL is re-validated through the exact same
  SELECT-only allowlist every other SQL in this codebase passes through
  — a malicious candidate (hypothetically, from a future LLM-assisted
  question-generation path) would be rejected identically to any other
  untrusted SQL.
- **Tenant isolation**: `OnboardingJob.tenant_id` + `onboarding.policy
  .authorize_onboarding_action`'s ABAC check (deny-by-default, cross-
  tenant denial checked *before* any RBAC branch) — the same scoped,
  forward-compatible pattern `identity.share_policy` already established
  for conversation sharing. A cross-tenant lookup resolves to the exact
  same 404 a genuinely nonexistent job would, never a distinguishable
  403 (anti-enumeration), verified by a dedicated HTTP-level test.
- **Performance**: Discovery's column-profiling pass is capped at 300
  columns per job (`_MAX_PROFILED_COLUMNS`) — a disclosed, deliberate
  bound, not an oversight, since an unbounded profile pass against a
  schema with thousands of columns would be a real cost/latency risk for
  a feature this prompt itself describes as needing to "minimize
  sensitive sampling." PII/relationship data-verification sampling is
  opt-in and bounded via `fetchmany()`, never a full scan. Publish's own
  evaluation pass executes only the SME-confirmed subset of golden
  questions, each row-capped and timeout-bounded identically to every
  other SQL execution in this codebase.

## 6. Known limitations / remaining risks

- **No background worker, which means no automatic resume.** A job that
  fails mid-`"discovering"`/`"publishing"` because the API call itself
  crashed (not a caught, job-marking-failed exception, but the process
  dying) is left in a non-terminal status with no automatic recovery —
  the operator must notice and call `/retry` with fresh credentials.
  This is the deliberate, disclosed cost of the "never persist a
  password" guarantee, not an oversight.
- **Golden-question SQL templates are intentionally simple** (count/
  aggregate-by-category/top-N/join-count) — they smoke-test that a
  database's basic shape is queryable, not that the LLM's eventual real
  generation for this database will be accurate. This is a materially
  weaker claim than `eval/`'s own execution-accuracy benchmark, and
  `onboarding/evaluation.py`'s own docstring says so explicitly.
- **No end-to-end verification against a real production-scale database**
  (real PostgreSQL/MySQL/SQL Server/Oracle) — every test here runs
  against real SQLite (chosen for the same reason `tests/` does
  elsewhere in this codebase: genuine SQL execution without a live
  external dependency), plus one full manual smoke test during
  development. The dialect-specific paths (percentile computation,
  timeout enforcement, identifier quoting) are themselves already
  covered by other prompts' own test suites against mocked dialect
  objects, not re-verified here against a second real engine.
- **`config/table_descriptions.yaml`/`config/sensitive_columns.yaml`
  application is manual** — `build_semantic_contract`'s output must be
  copy-pasted by an operator; a mismatched or already-hand-edited file
  could in principle be overwritten carelessly. No merge/diff tooling
  exists for this step.
- **A rejected review item leaves literally no trace** in the published
  semantic contract (by design, per rule 10) — but this also means there
  is no audit artifact distinguishing "never considered" from
  "considered and explicitly rejected" once published; that distinction
  only survives in the `OnboardingReviewItem` row itself, not in any
  exported artifact.

## Recommended next prompt

Per `02_TARGET_ARCHITECTURE.md` §5's own table: wire `analytics/`
(`ResultSummaryAnalyticsProvider`) into `generate_insight_node` and
surface `AnalyticsResult` in `AskResponse` — unchanged recommendation
carried forward from Prompts 04-07, still pending explicit sign-off
since it changes a live response shape.
