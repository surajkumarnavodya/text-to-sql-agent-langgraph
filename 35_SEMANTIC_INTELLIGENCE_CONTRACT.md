# Prompt 35 -- Advanced Semantic Intelligence

A deterministic analysis over the governed semantic catalog that finds synonym
candidates, term clusters, ambiguous terms, conflicting definitions, metric
relationships and candidate business rules. It also computes the impact of a
proposed change. Findings go into a review queue ordered by risk, not volume.
Behind `ENABLE_SEMANTIC_INTELLIGENCE` (default off).

## Inspected first

- **Catalog** (`semantic/catalog.py`, `identity/models.py::SemanticCatalogEntry`,
  `identity/repositories/semantic_catalog.py`): draft → reviewed → published →
  superseded, one row per version, tenant-scoped. The only conflict check
  (`find_conflicting_published_entries`) compared business names and synonyms
  only. It never compared definitions.
- **Metrics** (`semantic/metrics.py`, `data/knowledge/metrics.yaml`): governed
  metric definitions reach the SQL prompt through `retrieval`.
- **Glossary** (`data/knowledge/glossary.yaml`): curated, and disclosed in its
  own header as illustrative rather than reviewed. It is not an input to this
  engine, for that reason.
- **Onboarding inference** (`onboarding/semantic_inference.py`): column-level
  role inference with an ambiguity flag. Not reused, since it infers from column
  types and names and not from the catalog's own terms.
- **Authorization** (`semantic/catalog_policy.py`): `authorize_catalog_action`,
  deny-by-default, cross-tenant denied as not-found. Reused as-is.
- **Migrations**: head was `b7e2d4a1c9f0`. The new migration sits on top of it.

## Decisions

- **Deterministic, no LLM.** Every finding is therefore `AI_INFERENCE`, the
  output of a detector's comparison, never confirmed truth. Only a published
  catalog entry is `CONFIRMED_BUSINESS_TRUTH`, and a finding never changes one.
- **No fuzzy string similarity.** Edit-distance ratios match "date" to "update".
  Terms match on canonical equality (order-insensitive, plural-folded,
  abbreviation-expanded) or, for multi-word terms only, on token overlap of at
  least 0.75. A single shared word is never enough.
- **Stable finding keys.** A key hashes the kind and the subjects' concept keys,
  not row ids, so it survives new versions of a concept.
- **Versioned findings.** A re-run with unchanged content updates only
  `last_seen_at`. A change snapshots the old content into history, bumps the
  version, and reopens the finding for a fresh decision.
- **Risk ordering.** The score is a transparent sum of named parts: a base per
  kind, plus 15 per published concept (max two), plus 5 per dependent (max five),
  plus `(1 - confidence) * 20`. Tiers are high at 70 and above, medium at 40 and
  above, low otherwise. Each score returns its reasons.

## What was built

| Module | Does |
|---|---|
| `semantic/intelligence/normalize.py` | Canonical form, token overlap, `same_concept` (with the false-match guards). Cached. |
| `semantic/intelligence/detect.py` | Synonyms, term clusters, ambiguity, conflicts (`proposed_definition_differs`, `conflicting_metric_definitions`), candidate rules. |
| `semantic/intelligence/impact.py` | Metric relationships (`shares_source_tables`, `references_metric`) and `impact_of_change`, including proposed source-table removal. |
| `semantic/intelligence/risk.py` | The risk score, tiers, and risk-first ordering. |
| `semantic/intelligence/engine.py` | `run_analysis`: runs every detector, counts dependents, orders the queue. |
| `identity/repositories/semantic_intelligence.py` | Upsert with versioning, queue listing, accept/dismiss, finding rollback, catalog-version rollback as a new draft. |
| `identity/models.py::SemanticFinding` | The persisted finding, unique per `(tenant_id, database_id, finding_key)`. |
| `identity/migrations/versions/a1f5c9e3b7d2_semantic_intelligence.py` | Additive: one table, two indexes. Downgrade drops only that table. |
| `api/semantic_intelligence.py` | Six routes (below). |

Each finding carries: `kind`, `title`, `detail`, `subjects` (with each concept's
business `owner`, `status` and `version`), `evidence` (entry ids and the fields
that differ), `confidence`, `truth_level` (always `ai_inference`), `risk_score`,
`risk_tier`, `reasons`, `status` (open / accepted / dismissed), `version` and
history.

## API

All under `/semantic-intelligence`. Behind `ENABLE_SEMANTIC_INTELLIGENCE` (404 when off).

| Route | Permission | Does |
|---|---|---|
| `POST /run?database_id=` | review | Analyses one database's catalog and upserts up to the cap. Returns counts (created, updated, unchanged, truncated) and the top 10 findings. |
| `GET /findings?database_id=&status=` | review | The queue, riskiest first. |
| `POST /findings/{id}/decision` | review | `accept` or `dismiss` an open finding. A decided finding cannot be decided again (409). Review state only. |
| `POST /findings/{id}/rollback` | manage | Restores an earlier finding version as a new version, reopened for review. |
| `GET /impact/{concept_key}?database_id=&proposed_source_tables=` | review | Dependents, and what a source-table change would affect. Unknown concept is 404. |
| `POST /entries/{entry_id}/rollback` | manage | Creates a new DRAFT from an older catalog version. The published row is untouched. |

Authorization uses `semantic.catalog_policy.authorize_catalog_action`, the same
check the catalog routes run. A resource in another tenant returns 404, not 403.
A `database_id` the tenant may not use also returns 404. A user with no
resolvable tenant gets 403.

Request bodies forbid unknown fields. Decision accepts only `accept` or
`dismiss`. Responses carry `truth_level` on every finding.

## Configuration

| Setting | Env var | Default |
|---|---|---|
| `enable_semantic_intelligence` | `ENABLE_SEMANTIC_INTELLIGENCE` | `false` |
| `semantic_intelligence_max_findings` | `SEMANTIC_INTELLIGENCE_MAX_FINDINGS` | 500 |

The cap keeps the riskiest findings. Anything beyond it is reported as
`findings_truncated`, never silently dropped.

## Tests

- `tests/test_semantic_intelligence.py` (40): normalization, the false-match
  guards (`date`/`update`, `order date`/`order update`, `net sales`/`net sales
  amount` and others), synonyms, clusters and ambiguity, conflicts (cosmetic
  differences are not conflicts, superseded versions are never compared, conflicts
  are never auto-resolved), candidate rules, relationships and impact (including
  removal of a table), risk ordering, determinism, owner propagation, and that no
  finding is ever marked confirmed.
- `tests/test_semantic_intelligence_repo.py` (19): versioning and history
  (append-only), reopen-on-change, decision rules, finding rollback, tenant
  isolation at the repository layer, catalog rollback (creates a draft, never
  modifies an existing version), and engine-to-storage persistence.
- `tests/test_api_semantic_intelligence.py` (18): the flag, role gating (viewer
  refused, analyst cannot roll back, admin can), unknown database as 404, extra
  fields and invalid decisions as 422, a run that persists, the queue ordering,
  rerun idempotence, a second decision as 409, cross-tenant 404 on decide and
  an empty cross-tenant queue, the impact route, the persistence cap, and a
  user with no tenant as 403.

Results: 77 pass across the three semantic suites. The full backend suite passes
(3,993 tests) on the final code, including `tests/security/`. `ruff` and `mypy` are clean on all new code. Only a pre-existing mypy error in
`db/schema_introspection.py` remains. The migration's DDL was checked offline
with `alembic --sql`, which shows one table, two indexes and the head bump.

## Performance and cost

Measured on synthetic catalogs (`n` entries, every entry a metric sharing one of
20 tables, so this is the relationship-heavy case):

| Entries | Findings | Time (before caching) | Time (after caching) |
|---|---|---|---|
| 50 | 40 | 111 ms | 11 ms |
| 200 | 900 | 2,049 ms | 270 ms |
| 500 | 6,000 | 17,082 ms | 1,107 ms |

Caching the pure normalization functions gave about a 15x improvement. The
detectors are still pairwise, so cost grows roughly with the square of the number
of terms. A catalog of a few thousand terms would need blocking by canonical form
before the pairwise pass.

Persistence costs one SELECT per detected finding, capped at 500 per run. A run
is an explicit request, never run on the chat path, so it adds no latency to a
question.

## Limitations (honest)

- **No live catalog data.** Every test uses synthetic catalog entries. The
  engine has not been run against a real tenant's catalog.
- **Synonym discovery is conservative on purpose.** Abbreviations are a small,
  illustrative map. A real deployment extends it from a reviewed glossary. A
  synonym the map does not know is missed, which is the intended trade-off
  against false matches.
- **Pairwise cost.** See the performance section. Blocking by canonical form is the
  next step for large catalogs.
- **Stale findings are not auto-resolved.** A finding that stops being detected
  keeps its row and its status. Nothing marks it resolved. Re-running only
  updates findings the engine still produces.
- **Dependents count changes can version a finding.** Because `dependents` is
  part of a finding's content, a change to an unrelated concept can change a
  finding's score and bump its version. This is accurate, but it can be noisy.
- **Impact is a what-if list.** It is not a prediction of what will break.
  Dashboards, saved reports and golden examples are not in the graph, since the
  codebase has no such inventory.
- **Single-owner review.** There is no assignment workflow. A finding has a
  business `owner` per concept (from the catalog) and a `decided_by` reviewer,
  but no assigned reviewer.
- **Frontend not built.** Queue, decisions and impact are API-only. The SME
  dashboard (Prompt 27) is not extended to show these findings.
- **Ambiguity does not use the live schema.** A column name that matches two
  tables is not detected, because this engine reads the catalog and not
  introspected columns.

## Risks

- **Review fatigue** if the cap is too high or the risk weights are wrong. The
  weights are a first version, and tiers should be tuned on real review outcomes.
- **Finding text carries catalog content.** Business names, expressions and
  descriptions appear in `title` and `detail`. Catalog content is written by
  people, so it is untrusted text. A UI that renders it as HTML must escape it.
- **Process-independent state only in the database.** Nothing is cached across
  requests, so there is no cross-worker consistency problem to manage.

## Next prompt

**Prompt 36:** a live run of the engine against a real tenant's catalog (curated
test tenant, not production). Measure the true synonym, conflict and ambiguity
rates and the reviewer acceptance rate. Use that to tune the risk weights and
decide whether to add blocking for large catalogs and a schema-aware ambiguity
check.
