# 02 — Target Architecture & Incremental Migration

Prompt 02 of the 32-prompt Enterprise AI Analytics & Recommendation
Platform initiative (`00_MASTER_IMPLEMENTATION_CONTRACT.md`), building on
`01_BASELINE_ARCHITECTURE_AND_REUSE_INVENTORY.md`'s code-verified
baseline. **INSPECT/PLAN/IMPLEMENT (additive, inert only)** — no existing
route, node, or `Settings` field changed behavior; see §4 for exactly
what was added and §5 for what was deliberately deferred.

## 1. The 9 required boundaries, mapped to real code

| Boundary | Existing code (reused) | New in this prompt | Status |
|---|---|---|---|
| Experience/API | `api/` (67 real routes, verified in Prompt 01), `frontend/src/` | — | Reuse |
| Agent/LangGraph | `agent/graph.py` (12 nodes), `agent/orchestrator/` (router + 6 sources) | `tenant_id` plumbing only (§4.3) | Reuse |
| Semantic | `retrieval/` (7 `ChunkType`s, similarity-gated discovery) | `semantic/metrics.py` (governed metric definitions) | Reuse + new |
| Analytics | `agent/insight.py`'s `ResultSummary` (computed every run, `trend`/`outliers`/`stddev` never rendered) | `analytics/` (typed findings + a real adapter over that same data) | Extend |
| Recommendation | none | `recommendation/` (contracts only, no implementation) | New |
| Database Adapter | `db/connection.py::SUPPORTED_DB_TYPES`, `embeddings/retriever.py::select_database` | — | Reuse |
| Security/Governance | `agent/authz.py` (15 permissions), `identity/rbac.py`, `security/` | `agent/provenance.py` (DATABASE_FACT/AI_INFERENCE/CONFIRMED_BUSINESS_TRUTH) | Reuse + new |
| Persistence/Cache/Jobs | ORM (`identity/`), raw-SQL stores (`rag/`, `moderation/`, `feedback/`), ChromaDB, 6 `@cache`/`@lru_cache` singletons, bounded `ThreadPoolExecutor`s | — | Reuse |
| Observability/Evaluation | `observability/metrics.py`, `security/audit_log.py`, `eval/` | — | Reuse |

## 2. Target flow vs. today

```
NL question
  -> semantic understanding   [TODAY: retrieve_schema + retrieve_business_context (retrieval/)]
  -> intent                   [TODAY: classify_followup (standalone/followup/ambiguous) + agent.complexity's
                                       signal detection -- a real but narrow "how complex is this" signal,
                                       not a general intent classifier]
  -> analytical plan          [TODAY: plan_query_node -- only for complexity-flagged questions]
  -> governed metrics         [NOT WIRED: semantic/metrics.py exists (this prompt) but generate_sql_node
                                          does not consult it yet]
  -> SQL                      [TODAY: generate_sql_node]
  -> validation                [TODAY: agent/sql_validator.py -- unchanged, single source of truth]
  -> execution                 [TODAY: execute_sql_node]
  -> analytics                 [NOT WIRED: analytics/ exists (this prompt) but generate_insight_node
                                          does not consult it yet]
  -> insights                  [TODAY: generate_insight_node, agent.insight.is_insight_grounded]
  -> recommendations           [NOT WIRED: recommendation/ contracts exist (this prompt); no provider,
                                          no consumer]
  -> feedback                  [TODAY: embeddings/golden_examples.py, feedback/store.py]
```

Every "NOT WIRED" arrow above already has a real, tested, typed
foundation as of this prompt — see §4. None of them changes what a
question returns today.

## 3. Provenance vocabulary (contract rules 9-10)

`agent/provenance.py`'s `DataTruthLevel` (`DATABASE_FACT` /
`AI_INFERENCE` / `CONFIRMED_BUSINESS_TRUTH`) and `ProvenancedClaim` are
the shared vocabulary every future producer (insight narration, analytics
findings, a recommendation engine) should use, rather than each inventing
its own ad hoc confirmed/unconfirmed flag — see Prompt 01 §4's original
recommendation. `analytics/`'s findings are always `DATABASE_FACT` (a
pure adapter over Python-computed statistics); `recommendation/`'s
`Recommendation.claim` is type-enforced to always be `AI_INFERENCE`
(a Pydantic validator rejects any other level at construction time) —
the closest thing to a compile-time guarantee against rule 10's "never
silently promote inference to confirmed truth" this shape allows.

## 4. What was actually built (additive, inert)

### 4.1 `agent/provenance.py`
`DataTruthLevel` enum + `ProvenancedClaim` frozen model. Not retrofitted
onto `agent/insight.py`'s own output in this prompt.

### 4.2 `analytics/` (`models.py`, `provider.py`)
`AnalyticsFinding`/`AnalyticsResult` + `AnalyticsProvider` Protocol +
`ResultSummaryAnalyticsProvider`, the one real default implementation —
adapts `agent.insight.ResultSummary.trend`/`.outliers`/per-column
`stddev` (all already computed by `summarize_result`, already folded into
`is_insight_grounded`'s allowed-value sets, never rendered anywhere) into
typed findings. Zero new statistics computed — proven by
`tests/test_analytics_provider.py::test_zero_new_computation_findings_are_a_pure_subset_of_summary_data`.
**Not called by `agent.nodes.generate_insight_node` or any route.**

### 4.3 `recommendation/` (`models.py`, `provider.py`)
`Recommendation` + `RecommendationProvider` Protocol only — no default
implementation, since no recommendation logic exists anywhere in this
codebase to adapt (verified: Prompt 01's repo-wide inspection found none).
**Not called anywhere.**

### 4.4 `semantic/metrics.py`
`MetricDefinition`/`MetricStatus`/`MetricRegistry` Protocol +
`YamlMetricRegistry`, a real default implementation loading the same
`data/knowledge/metrics.yaml` `retrieval/chunking.py::metric_chunks_from_yaml`
already parses for fuzzy retrieval — the same source file, read
independently, never the reverse. Every loaded definition defaults to
`MetricStatus.DRAFT`/`owner=None`, disclosed rather than invented: no
human-approval workflow exists in this codebase today. **Deliberately not**
an 8th `retrieval.models.ChunkType` or a reuse of `Chunk.extra` — see
§4.4.1. No new persistence, no approval-workflow API. **Not called by
any node or route.**

#### 4.4.1 Why a dedicated module, not an 8th `ChunkType`
`retrieval/`'s `METRIC` chunk type is real but serves a fundamentally
different access pattern than governed metrics: it's similarity-
threshold-gated discovery (a metric chunk only reaches a prompt if it
scores well against the question text — it can silently not show up),
and its type-specific fields live in a deliberately untyped
`extra: dict[str, Any]`. Governance fields (owner, approval status,
supersession) need authoritative typed resolution, which that model
can't honestly provide without overloading `extra`'s "just rendering
hints" contract or conflating `Chunk.version` (re-ingestion/idempotency)
with a governance version. The natural later integration (a **future**
prompt, not this one) is retrieval-*consumes*-semantic: an adapter
rendering a `MetricDefinition` into a `METRIC` chunk's `extra` for
discovery purposes.

### 4.5 Tenant context propagation (plumbing only, no enforcement)
`security/tenancy.py` gained `resolve_tenant_id_for_identity(identity:
AuthIdentity | None) -> str | None` — a sibling to the existing
`resolve_actor_tenant_id(user: User | None)`, correctly typed against
`security.oidc.AuthIdentity` rather than `identity.models.User` (verified:
`api/main.py`'s `/ask` handler only ever has an `AuthIdentity` in scope,
never a `User`, so the existing function alone would have been a type
mismatch at that call site).

`AgentState` (`agent/state.py`) gained `tenant_id: str | None`, following
the identical "resolved once by the caller, read-only downstream,
request-scoped, never a process-global" documented contract
`selected_database`/`selected_model` already establish. Threaded through
both `agent.graph.run_agent` and `agent.orchestrator.graph.run_orchestrated`
(a deliberate deviation from the approved plan's literal wording, which
suggested mirroring `caller_subject`'s narrower, orchestrator-only
threading — verified `caller_subject` is *not* a `run_agent` parameter at
all, since it's only meaningfully used by the orchestrator's own
cost-ceiling scoping, whereas tenant context is meaningful on the base
SQL-only path too) and resolved in `api/main.py`'s `ask()` handler
alongside the existing `caller_roles=`/`caller_subject=`.

**Not enforced anywhere new.** The only real tenant enforcement in this
codebase remains `identity.share_policy.authorize_share_action`, which
resolves its own tenant id independently via `resolve_actor_tenant_id`
and does not read `AgentState["tenant_id"]`. This field exists so a
future prompt that adds genuine multi-tenant checks to the SQL/analytics
path doesn't need another `AgentState` migration to get a tenant id to
check against.

## 5. Stubbed today, wired later — explicit table

So none of these go stale the unnoticed way `agent/tools/` risked (built,
tested, no named revisitation commitment):

| Module | What it needs to become live | Suggested trigger |
|---|---|---|
| `agent/provenance.py` | Retrofit `agent.insight`'s `ResultSummary`/insight output to construct `ProvenancedClaim`s instead of a bare grounded/ungrounded boolean | The prompt that generalizes `is_insight_grounded` |
| `analytics/` | Call `ResultSummaryAnalyticsProvider().analyze(...)` from `generate_insight_node` (or a new node) and surface `AnalyticsResult` in `AskResponse` | The prompt that adds a UI trend/outlier badge, per `docs/DEEP_FEATURE_PERFORMANCE_ASSESSMENT.md`'s own existing P1 roadmap item |
| `recommendation/` | Design and build the first real `RecommendationProvider` | A dedicated future prompt — no existing logic to adapt, so this is genuinely new engineering, not wiring |
| `semantic/metrics.py` | (a) retrieval renders a `MetricDefinition` into a `METRIC` chunk's `extra`; (b) a real approval-workflow/CRUD surface sets `owner`/`APPROVED` | Two separate future prompts — do not conflate |
| `AgentState["tenant_id"]` | A node/route reads it and enforces a real tenant-match check (mirroring `identity.share_policy`'s pattern) | The prompt that extends tenant isolation beyond conversation sharing |

## 6. Explicitly out of scope for this prompt

- Wiring any new node into `agent/graph.py` or `agent/orchestrator/`.
- Any new API endpoint or `Settings`/env var.
- Real tenant *enforcement* outside conversation sharing.
- A metrics-governance approval workflow/CRUD API.
- Retrofitting `agent/insight.py`'s output with `agent/provenance.py`'s types.

## 7. Verification

Full regression suite (`pytest`, `vitest`, `ruff`/`black`/`mypy`,
`tsc`/`oxlint`/`npm run build`) run after implementation — results in this
prompt's own closeout report (`00_MASTER_IMPLEMENTATION_CONTRACT.md`'s
required 12-point format), not duplicated here.
