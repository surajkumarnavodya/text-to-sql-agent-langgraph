# 09 — Tenant-Aware Semantic Catalog

Prompt 09 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), following
`08_ONBOARDING_ENGINE_CONTRACT.md`. **INSPECT/PLAN/IMPLEMENT/TEST/
REVIEW/FIX/DOCUMENT** — a new `semantic/catalog.py`+`catalog_policy.py`
(typed model + ABAC, zero DB dependency), a new `identity/` persistence
slice (1 ORM table + Alembic migration + repository module, following
`identity/repositories/onboarding.py`'s own split exactly), a new RBAC
permission pair, a new `ChunkType.BUSINESS_CONCEPT` + two small
`retrieval/` extensions (never a second retrieval stack), a new planning-
prompt renderer, and a new REST surface (`api/semantic_catalog.py`). No
existing route, node, chunk type, or `Settings` field changed behavior
for a caller that doesn't use the new capabilities.

## 1. Inspection: what already existed vs. the real gaps

`retrieval/` already had a 7-type (`ChunkType`), per-database-Chroma-
collection RAG pipeline with its own retriever/reranker/ingestion,
already injected into `generate_sql_node`'s prompt
(`agent.llm_client._build_business_context_block`) and, for relationship
chunks specifically, into `plan_query_node`'s prompt
(`_build_plan_relationship_block`, Prompt 07). `semantic/metrics.py`
already had `MetricDefinition`/`MetricStatus` (draft/approved/
deprecated)/`MetricRegistry`, but flat, global, not tenant-aware, and
with no real approval workflow — `02_TARGET_ARCHITECTURE.md` itself
named this as two explicit future gaps: "(a) retrieval renders a
governed `MetricDefinition` into a `METRIC` chunk's `extra`" and "(b) a
real approval-workflow/CRUD surface sets `owner`/`APPROVED`." `agent/
provenance.py` already had the `DataTruthLevel` vocabulary this prompt
needed, reused (always `AI_INFERENCE`) by Prompt 07/08's own inference
dataclasses. `onboarding/policy.py` + `identity/repositories/
onboarding.py` already established the exact RBAC+ABAC+plain-CRUD split
this prompt's own review workflow needed.

Real gaps, found by reading the actual code (not assumed):

1. **No single typed concept covering names/descriptions/grain/keys/
   relationships/domains/dimensions/synonyms/business rules/examples/
   evidence/confidence/status/owner/version together** — `glossary.yaml`
   covered synonyms+definition only; `MetricDefinition` covered metrics
   only; `TableDescription`/`ColumnClassification` were untyped-business-
   metadata, global, no status/owner/version at all.
2. **No draft/reviewed/published/superseded lifecycle anywhere** —
   `MetricStatus` has a different 3-value vocabulary (draft/approved/
   deprecated) with no intermediate "reviewed but not yet live" state.
3. **No tenant dimension on any retrieval/business-metadata construct** —
   `Chunk` has no `tenant_id`; `config/table_descriptions.yaml`/
   `sensitive_columns.yaml` are single global files.
4. **No review API** — nothing catalog-specific existed to create,
   review, or publish a business concept.
5. **Planning never saw anything beyond relationship-type retrieved
   context** — confirmed by reading `generate_query_plan_from_llm`'s
   actual signature before this prompt.

## 2. What was built

### `semantic/catalog.py` (new)

`CatalogConceptType` (entity/metric/dimension/domain), `CatalogStatus`
(draft/reviewed/published/superseded — the exact vocabulary this prompt
specifies, deliberately distinct from `MetricStatus`), `VALID_STATUS_
TRANSITIONS` (the single source of truth both the repository and the API
layer check), `status_to_truth_level` (draft/reviewed → `AI_INFERENCE`,
published → `CONFIRMED_BUSINESS_TRUTH`, superseded → `AI_INFERENCE`, never
re-promoted — the structural enforcement of master rules 9-10),
`CatalogEntrySnapshot` (the ORM-independent read shape — mirrors
`onboarding/semantic_contract.py::ConfirmedReviewItem`'s identical "no
identity/ORM dependency" precedent, so `retrieval/` never needs
SQLAlchemy on its import graph), `metric_definition_from_snapshot` +
`build_metric_registry_from_catalog` (the explicit bridge closing
`02_TARGET_ARCHITECTURE.md`'s named gap "(b)" — a `METRIC`-type published
entry becomes a real `MetricDefinition` with `status=APPROVED`, reusing
`YamlMetricRegistry`'s own constructor as a second, richer data source
for the same `MetricRegistry` Protocol, not a new registry class).

### `semantic/catalog_policy.py` (new)

`CatalogAction` (create/view/edit_draft/review/request_changes/publish),
`AuthorizationDecision(allowed, reason)`, `authorize_catalog_action` —
mirrors `onboarding/policy.py` exactly: deny-by-default, cross-tenant
denial checked before any RBAC branch, `CATALOG_MANAGE` (admin-tier:
create/edit/publish) vs. `CATALOG_REVIEW` (analyst-tier: review/
request-changes), either permission for viewing.

### `identity/` additions

- **`models.py`** — `SemanticCatalogEntry` (23rd table): one row per
  **version** of one concept, never mutated in place once published (an
  edit always creates a new draft row; publishing flips the prior
  published row to `superseded` + links `supersedes_id`) — the fusion of
  `OnboardingArtifact`'s append-only versioning with `MetricDefinition
  .supersedes`'s chain. `tenant_id` + `database_id` scoped, following
  `OnboardingJob.tenant_id`'s exact precedent.
- **`migrations/versions/d5e9f2a7b4c1_semantic_catalog.py`** — chains
  after Prompt 08's onboarding migration (confirmed via `alembic history`
  resolving to a single head).
- **`rbac.py`** — `Permission.CATALOG_MANAGE`/`CATALOG_REVIEW`, seeded,
  `CATALOG_REVIEW` added to `_ANALYST`, `CATALOG_MANAGE` to `_ADMIN`.
- **`repositories/semantic_catalog.py`** — plain CRUD, zero authorization
  logic: `create_entry` (computes next version via `next_version_for_
  concept_key`), `get_entry_by_id`, `list_entries`, `list_versions_for_
  concept_key`, `update_draft_entry` (draft-only), `mark_reviewed`
  (draft→reviewed), `request_changes` (reviewed→draft), `publish_entry`
  (reviewed→published, handles supersession, returns the superseded row
  too), `entry_to_snapshot`. Defends every transition against
  `semantic.catalog.VALID_STATUS_TRANSITIONS` itself
  (`InvalidCatalogStatusTransitionError`) — defense in depth behind the
  API layer's own pre-flight check, since a wrong transition here would
  corrupt a concept's whole version/supersession history, not just one
  review-item decision.

### RAG integration — the acceptance-criterion-critical piece

**New `ChunkType.BUSINESS_CONCEPT`** (`retrieval/models.py`) —
deliberately additive, not a reuse of `GLOSSARY`/`METRIC`: a governed
catalog entry carries structured governance fields (status/owner/
confidence/evidence/version) a hand-authored YAML chunk never does, and
conflating the two under one type would make them indistinguishable
during rerank/labeling (the same reasoning `02_TARGET_ARCHITECTURE.md`
already gives for keeping `semantic/metrics.py` separate from
`ChunkType.METRIC`). Confirmed safe to add: no test anywhere enumerates
`ChunkType` exhaustively.

- **`retrieval/chunking.py`** — `business_concept_chunk_from_catalog_
  entry(snapshot, embedding_model, embedding_dimensions) -> Chunk`: a
  self-contained text rendering (business name, description, grain,
  keys, relationships, domain, synonyms, business rules, examples) plus
  a fully-populated `extra` (concept_type/concept_key/status/confidence/
  evidence/owner/version/truth_level). `chunk_id` is made unique per
  **version** via the existing `make_chunk_id`'s `version` parameter — a
  new published version never collides with the one it supersedes.
- **`retrieval/ingestion.py`** — `sync_catalog_entry_to_vector_store`
  (embeds + upserts the new chunk, deletes a given superseded chunk id,
  via the *existing* `EmbeddingProvider`/`VectorStore` primitives —
  zero new embedding/upsert logic) and `business_concept_chunk_id_for_
  snapshot` (lets a caller derive a superseded version's own chunk id
  without re-deriving `make_chunk_id`'s argument order). Called
  **synchronously** from the publish API route, not a batch job — a
  newly-published concept is searchable immediately.
- **`retrieval/retriever.py`/`reranker.py`/`config/settings.py`** — new
  `retrieval_top_k_business_concepts` setting, `_per_type_top_k` entry,
  `_TYPE_PRIORITY` entry (0.95, alongside relationship/glossary tier).
- **`agent/llm_client.py`** — a `_BUSINESS_CONTEXT_TYPE_LABELS` entry
  (zero other change to `_build_business_context_block`, which already
  handles any chunk type generically) and, **for planning specifically**
  (the acceptance criterion's literal wording), a new
  `_build_plan_business_concept_block` wired into `_build_plan_user_
  prompt` alongside the existing relationship block — this is what makes
  `plan_query_node`'s own LLM call, not just `generate_sql_node`'s, see
  governed business concepts.

**The structural enforcement of "never silently convert AI inference
into confirmed business truth" (master rule 10):** only a `PUBLISHED`
entry is ever converted into a chunk — `business_concept_chunk_from_
catalog_entry`/`sync_catalog_entry_to_vector_store` are only ever called
from the publish route. A draft or reviewed entry simply never reaches
retrieval, by construction, not by a runtime check that could be
forgotten.

### REST API — `api/semantic_catalog.py` + `api/semantic_catalog_schemas.py` (new)

Mirrors `api/onboarding.py`'s shape exactly (same `_require_entry`/
`_authorize`/`_entry_out` helper pattern, same anti-enumeration cross-
tenant→404 mapping): `POST /semantic-catalog/entries` (create draft),
`GET /semantic-catalog/entries` (list, tenant-scoped, filters), `GET
/semantic-catalog/entries/{id}`, `GET /semantic-catalog/entries/concept/
{concept_key}/versions` (history), `PATCH /semantic-catalog/entries/{id}`
(edit draft), `POST .../review` (draft→reviewed), `POST .../request-
changes` (reviewed→draft), `POST .../publish` (reviewed→published +
vector-store sync + supersede). A vector-store sync failure on publish
does **not** roll back the already-committed status change — logged and
treated the same "retrieval is best-effort, never a hard gate"
philosophy `retrieval.retriever.retrieve_business_context` already
establishes (see §6 for the disclosed limitation this creates).

## 3. Explicitly out of scope

- **No cross-reference with `config/table_descriptions.yaml`/
  `sensitive_columns.yaml`** — those remain exactly as disclosed in
  Prompt 06/08's own notes (global, not tenant/database-scoped); this
  prompt adds a parallel, properly-governed catalog rather than
  retrofitting the older YAML files.
- **No UI** — this prompt is the typed model + persistence + RBAC/ABAC +
  REST surface + retrieval integration only, matching every other
  backend-only prompt in this series (05-08) before a frontend prompt
  consumes it.
- **No automatic promotion from an inference pass** (e.g. onboarding's
  own `SemanticLabel`) into a catalog entry — an SME or operator creates
  entries explicitly via the API today; wiring `onboarding/jobs.py`'s
  discovery output into a draft catalog entry automatically is a
  reasonable future prompt, not attempted here (keeps this prompt's own
  scope bounded to the catalog itself).
- **No batch/backfill resync** for a publish whose vector-store sync
  failed (see §6) — a disclosed, deliberate scope boundary, not an
  oversight.
- **Domain entries have no dedicated "list concepts under this domain"
  endpoint** — `domain` is a free-text field on any entry (mirroring
  `glossary.yaml`'s own free-text `tags`), filterable via the existing
  `concept_type=domain` query param on `GET /entries`, but no graph/
  hierarchy traversal exists.

## 4. Testing

7 new test files, ~91 tests, all passing:

- `tests/test_semantic_catalog_model.py` (17) — `status_to_truth_level`'s
  full mapping (including superseded never re-promoted),
  `VALID_STATUS_TRANSITIONS`'s every entry, `metric_definition_from_
  snapshot`'s full field mapping + status bridge, `build_metric_registry_
  from_catalog`'s filtering/lookup.
- `tests/test_semantic_catalog_policy.py` (15) — every ABAC/RBAC branch,
  cross-tenant-before-RBAC priority, mirroring `test_onboarding_policy
  .py`'s exact coverage shape.
- `tests/test_identity_repositories_semantic_catalog.py` (19) — real
  in-memory SQLite: version incrementing per concept_key, every status
  transition (including the three illegal ones raising), full publish→
  supersede→list-versions round trip, snapshot field round-tripping.
- `tests/test_retrieval_catalog_chunking.py` (10) — chunk shape/text/
  extra correctness, chunk-id uniqueness per version, no-collision
  across concept types sharing a key, relationship rendering.
- `tests/test_retrieval_catalog_sync.py` (8) — real upsert/delete via
  `InMemoryVectorStore`, supersession removing the old chunk while the
  new one lands, collection auto-creation, a vector-store failure
  propagating as `VectorStoreError`.
- `tests/test_llm_client_planning.py` (+9) — the new business-concept
  plan-block renderer (filtering, DATA-framing, the governed/published
  wording) and its coexistence with the relationship block in
  `_build_plan_user_prompt`.
- `tests/test_api_semantic_catalog.py` (13) — full HTTP lifecycle via
  `TestClient` with the real in-memory-SQLite-identity-DB pattern:
  RBAC (admin creates, plain user/analyst-alone forbidden), tenant
  isolation (cross-tenant lookup is a 404, not 403), the full create→
  review→publish lifecycle **actually landing a chunk in a fake vector
  store only after publish, never before**, request-changes sending a
  reviewed entry back to editable draft, every illegal transition
  (409), and publishing a second version superseding the first **in
  retrieval itself** (chunk count stays at 1, old chunk gone, new one
  present, `GET .../versions` reporting the correct history).

**Full suite: 2724 tests pass, zero regressions** (`pytest -q`, re-run
after every change in this prompt; 91 of those are new — 82 across the 6
brand-new test files plus 9 added to the existing `tests/test_llm_client
_planning.py`). `ruff check` / `black --check` / `mypy`
on every touched/new production and test file: clean (one pre-existing,
unrelated `api/main.py` finding and 6 pre-existing `tests/test_llm_client
_planning.py` findings confirmed via `git stash` to predate this prompt's
changes — left alone, not claimed as fixed, per `CLAUDE.md`'s own
documented CI-hygiene-gap tracking).

## 5. Security / tenant-isolation / performance review

- **Security**: A draft/reviewed entry structurally never reaches
  retrieval (only `publish` calls the chunk-rendering/sync functions) —
  the live enforcement of "never silently convert AI inference into
  confirmed business truth," not a convention that could be bypassed by
  a future caller forgetting a status check. `authorize_catalog_action`
  is deny-by-default at every branch, unit-tested directly with no
  mocking. The publish route never accepts a client-supplied tenant_id
  or chunk id — both are always derived server-side
  (`resolve_actor_tenant_id`, the repository's own returned rows).
- **Tenant isolation**: `SemanticCatalogEntry.tenant_id` + `semantic
  .catalog_policy.authorize_catalog_action`'s ABAC check (cross-tenant
  denial checked before any RBAC branch, mapped to the same 404 a
  genuinely nonexistent entry would get) — the same scoped,
  forward-compatible pattern `identity.share_policy`/`onboarding.policy`
  already established. Verified end-to-end via a real HTTP test (not
  just the policy unit test) using the established two-tenant
  email-domain simulation.
- **Performance**: Publishing triggers exactly one embed call + one
  upsert + (optionally) one delete — the same per-publish cost
  `onboarding/jobs.py`'s own golden-question evaluation already accepts
  as proportional to the action taken, not a batch/background cost.
  Listing/filtering entries is a single indexed query
  (`tenant_id`/`database_id`/`concept_type`/`status` are all indexed
  columns). No N+1 or unbounded query exists anywhere in the repository
  layer.

## 6. Known limitations / remaining risks

- **A vector-store sync failure on publish doesn't roll back the
  identity-DB status change.** The entry is genuinely `"published"` in
  the database of record but may not yet be searchable if the sync call
  failed (network blip, embedding-model unavailability). There is
  currently no automated resync/backfill path for this — an operator
  would need a future dedicated script (mirroring `scripts/rebuild_
  index.py`'s own "explicit, operator-initiated" posture) to re-sync a
  known-published-but-unindexed entry. Disclosed, not silently accepted
  as resolved.
- **No cascade from a superseded entry's own retrieval history** — once
  a new version is published, the superseded version's chunk is removed
  from the vector store, but if a past conversation already retrieved
  and cited that older chunk's text, nothing updates that historical
  record (the same disclosed limitation `identity.repositories.history`
  already has for schema/DDL shown at generation time — see `CLAUDE.md`'s
  chat-history "Known limitations").
- **`domain` is free text, not a governed entry of its own with real
  referential integrity** — a `DOMAIN`-type catalog entry and another
  entry's free-text `domain` field are never cross-validated against
  each other. A typo in one doesn't break anything, but also isn't
  caught.
- **No live-LLM verification that a published business concept
  measurably improves plan/SQL quality** — this prompt's own tests
  verify the *plumbing* (the concept reaches the prompt), not a
  benchmark showing improved accuracy; that would require re-running
  `eval/`'s benchmark with and without catalog entries populated, out of
  scope here (the eval harness itself doesn't exercise business-context
  retrieval at all yet — a pre-existing, separately-disclosed gap).

## Recommended next prompt

Per `02_TARGET_ARCHITECTURE.md` §5's own table: wire `analytics/`
(`ResultSummaryAnalyticsProvider`) into `generate_insight_node` and
surface `AnalyticsResult` in `AskResponse` — unchanged recommendation
carried forward from Prompts 04-08, still pending explicit sign-off since
it changes a live response shape. Alternatively, closing this prompt's
own §6 vector-store-sync-failure gap with a small backfill script would
be a natural, low-risk immediate follow-up.
