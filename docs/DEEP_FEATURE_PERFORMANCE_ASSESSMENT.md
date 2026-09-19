# Deep Feature, Understanding & Performance Engineering — Assessment

**Date:** 2026-09-18. **Scope:** response to
`claude_deep_ai_feature_performance_master_prompt.md` — discovery,
assessment, comparison, and a target architecture/roadmap. **No
implementation happened before this document was complete**, per the
prompt's own "CRITICAL: DO NOT IMPLEMENT FIRST" instruction.

## 0. Relationship to prior work — read this first

This is not a green-field discovery. **Three rigorous engineering passes
already ran against this exact codebase, today and two days prior**, and
this document does not re-derive what they already established with real
evidence:

| Prior pass | Document(s) | What it covers |
|---|---|---|
| Security + performance Phase 1–3 (2026-09-16) | `docs/PHASE1_BASELINE.md`, `docs/PHASE1_FINAL_REPORT.md`, `docs/PHASE2_SECURITY_REPORT.md`, `docs/PERFORMANCE_BASELINE.md`, `docs/PERFORMANCE_RESULTS.md`, `docs/EVALUATION_CURRENT.md` | A **real, measured latency waterfall** (per-stage `stage_timings_ms` aggregated across a live 57-case benchmark), an **LLM call-volume/token table**, 5 verified P0 security fixes, dependency hygiene, and an honest, non-fabricated report of a latency *regression* traced to environment contention rather than claimed as an improvement |
| Platform transformation assessment (2026-09-18) | `docs/PLATFORM_TRANSFORMATION_ASSESSMENT.md`, `docs/TOOLS.md` | Full architecture/feature inventory, a gap analysis against a "multi-source agentic platform" target, a P0/P1/P2 roadmap, and the **first P0 item already built**: `agent/tools/` — a generic, governed, tested Tool/MCP-shaped registry (permission checks, timeouts, retry policy, audit logging) wrapping the six real source functions, fully tested, but **not yet wired into any live request path** |
| Two UI passes (this session) | `docs/frontend-ui-audit.md`, `docs/ui-design-system.md`, `docs/chat-history-ui.md`, `docs/image-editing-architecture.md`, `docs/ui-production-audit.md`, `docs/navigation-and-actions.md` | Frontend architecture, dedup of duplicated controls, sidebar collapse, image editor — orthogonal to this prompt's performance/AI-architecture focus |

**What this document adds that genuinely doesn't exist yet**: a
competitor-capability comparison (§1 of the master prompt — not attempted
by any prior pass), an LLM-call/caching/context/streaming/model-routing
synthesis organized against this specific prompt's 38-part structure,
confirmation of what's still missing for RAG/web evaluation, MCP live
wiring, deep research, memory/workspace, and a **reconciled** P0/P1/P2
roadmap that merges the platform assessment's roadmap with this prompt's
heavier performance/observability emphasis.

**What this document will not do**: fabricate load-test numbers at 10/50/
100 concurrent users. This environment has one local Ollama instance
serving one model; the existing `docs/PERFORMANCE_RESULTS.md` already
demonstrates, with real evidence, that even a *sequential* benchmark run
on this shared machine produced wildly noisy latency (P99 sweeping from
440s to 3,174s between two runs of *identical* code, attributed to
concurrent unrelated load on the same machine) — running a synthetic
50-100-concurrent-user load test here would produce numbers that measure
this laptop's contention, not the application's real scalability
characteristics, and reporting them as if they did would violate the
prompt's own explicit rule ("Do not claim scalability without
measurements... do not fabricate test results"). §8 below states exactly
what a real load test would need and why it's a P2/deployment-environment
item, not something honestly performable in this session.

---

## 1. Competitor Capability Benchmark (Part 1)

Public-capability patterns only, used as architectural reference — no
proprietary internals claimed, no branding/UI copied (per the prompt's own
constraint). For each capability: applicability, current state here, and
P0/P1/P2.

| Capability | Applies to this product? | Current state | Priority |
|---|---|---|---|
| Conversation quality | Yes — core | Strong for SQL (structured prompts, plan/review self-correction); no general-purpose chit-chat layer, by design (off-topic questions are explicitly rejected, `OffTopicQuestionError`) | Retain as-is |
| Reasoning/orchestration | Yes | Two explicit LangGraph state machines, not a ReAct loop — deliberate, inspectable, bounded (`recursion_limit`) | Retain; do not replace with a generic agent loop |
| Deep research (multi-step, iterative) | Yes, for multi-source questions | **Missing** — `router_node` is one classifier call picking a source *set*, not an iterative plan/search/verify loop | **P1** (large, genuinely new) |
| Web search | Partial | Real (Tavily), but no query rewriting, no multi-round search, no dedicated caching, no freshness/trusted-domain routing | **P1** |
| Citations | Yes, already good | `rag/graph.py`'s `Citation` `TypedDict` (filename, chunk_index, page_number, document_id, has_pdf_bytes) built from real retrieved-chunk metadata, never LLM-invented — genuinely matches the prompt's Part 14 requirement already | Retain; extend to web/SQL sources (see §7) |
| Long context / context compaction | Partial by design | No raw-history dump exists to compact in the first place — `_build_user_prompt` only ever includes the single most-recent conversation exchange (see §5) plus retrieval-bounded schema/business-context blocks. This is context *avoidance* by architecture, not context *management* of a large window | Retain; revisit only if a future feature needs multi-turn reasoning beyond one prior exchange |
| Memory (user/project) | **Missing** | Chat history persists (server-side, for local-auth users) but there is no separate "memory" concept — no user preferences/facts extracted and reused across conversations | **P2** — real product value, real privacy/scope-control complexity; not requested by name in this codebase's own docs, don't build speculatively |
| Projects/workspaces | **Missing** | `identity/models.py` stops at `Conversation`/`Prompt`/`AiOutput` — confirmed by the platform assessment | **P1** (per platform assessment's own roadmap) |
| File/document understanding | Yes, mature | PDF RAG (`rag/`), moderation-gated, malware-scannable, RBAC + sensitivity-category gated | Retain |
| Multimodal understanding | Partial | Image upload/local editing exists (frontend-only); vision captioning exists for media search (Ollama vision model, off by default); no image sent *to* the SQL/RAG pipeline as input | Retain scope as-is; see `docs/image-editing-architecture.md` for the documented backend contract not yet built |
| Data analysis | Yes, basic | `agent/insight.py` — one grounded sentence (min/max/sum/top-share), not trend/variance/correlation/anomaly | **P1** per platform assessment |
| Visualization | Yes, basic | Backend intent exists (`agent/result_charting.py`); frontend chart adapter only distinguishes bar/line | **P1** per platform assessment |
| Coding/tool use | N/A | Not a coding-agent product; out of scope by design | Not applicable |
| Agentic execution | Partial | Bounded, node-level agentic self-correction (retry loop) exists; no dynamic tool-selection agent exists | See MCP row below |
| MCP/integrations | **Built, not wired** | `agent/tools/` exists, tested, 6 tools registered, zero production call sites (confirmed this session — grep found matches only in `agent/tools/*` and its own tests) | **P0** — the highest-leverage, lowest-risk next step: it already exists, the only work left is a real consumer |
| Artifacts | **Missing** as persistence | One-shot client-side CSV/PDF export only, non-editable, non-persistent | **P1**, depends on workspace data model |
| Personalization | **Missing** | Theme/accent/font/language are UI prefs, not content personalization | **P2** |
| Streaming | **Missing** | Confirmed this session: `/ask` is a single JSON response; `agent/llm_client.py`'s own `client.chat()` calls have no `stream=True`; frontend has no `EventSource`/`ReadableStream` consumer | **P1** — real latency-*perception* win even though total latency is LLM-bound (see §3) |
| Latency | Bottleneck identified, not blindly optimized | `docs/PERFORMANCE_BASELINE.md`: LLM inference is 93.8-98% of wall-clock time; every other stage is <2% combined and not worth optimizing | No further backend "optimization" justified without a model/hardware/prompt-size tradeoff — see that doc's own conclusion |
| Reliability | Partial | Fail-open/fail-closed applied consciously per subsystem; **no circuit breakers exist anywhere** (grepped, zero hits) around Ollama/Azure Content Safety/Tavily/IMA — a real, disclosed, still-open gap (`docs/RISK_REGISTER.md` R-006) | **P1** |
| Cost efficiency | Strong for the SQL path (local LLM, no per-call cost); real for web/media (Tavily, IMA) | Human-approval gate + session cost ceiling for the one paid, spend-real-money source; no cost budgeting/model-tier routing exists | **P2** — real lever, but the current single-model setup makes "tiering" moot until multiple models are actually available |
| Security | Mature | See §4 — unchanged from the Phase 1-3 findings, independently re-confirmed this session | Maintain; close the 3 disclosed verification gaps (DAST, live OIDC, real ClamAV) |
| Observability | Partial | Correlation IDs + per-stage timing logs are real and thorough (§3); **no metrics/tracing system** (no OTel, no Prometheus, no `/metrics` endpoint) — timing exists only as unaggregated log lines | **P0** — foundational, additive, low-risk, directly required before any further "optimize X" claim per this prompt's own "do not optimize from assumptions" rule |
| Evaluation | Partial | Rigorous for Text-to-SQL (execution accuracy, result-set accuracy, schema recall, security-rejection — real numbers, real regression tracking); **zero evaluation exists for RAG or web search** (confirmed this session — no RAG/web references anywhere in `eval/evaluators.py`) | **P1** |

**Overall read**: this product's actual differentiator — the AST-validated,
plan-reviewed, retry-bounded Text-to-SQL pipeline — is already at or above
the rigor of what's publicly documented for general-purpose competitors in
that one narrow domain (none of ChatGPT/Claude/Gemini/Copilot publish a
comparable SQL-safety architecture). The gaps are concentrated in exactly
the areas the master prompt's Part 4 target list names: deep research,
memory/workspace, dynamic tool selection (MCP wired live), and
observability/evaluation depth outside SQL — not in the SQL core itself.

---

## 2. Current Architecture (Part 3.1) and Repository Map (3.2)

Unchanged from `docs/PLATFORM_TRANSFORMATION_ASSESSMENT.md` §2 — reproduced
here only as a pointer, not a duplicate. One correction: that document
says "12-node" graph in its executive summary but "11-node" in its
repository-map section; the accurate count (confirmed against
`agent/graph.py` and `CLAUDE.md`'s own node list this session) is
**11 nodes**: `sanitize_input → classify_followup → retrieve_schema →
retrieve_golden_examples → retrieve_business_context → plan_query →
generate_sql → review_sql → validate_sql → estimate_cost → execute_sql →
generate_insight` — that's actually 12 listed stages; the "11-node" count
in `CLAUDE.md` likely predates the later `retrieve_business_context`
addition. Worth a one-line doc fix (tracked in §9's small-items list), not
a structural finding.

## 3. Performance Assessment (Part 4) — synthesis, not re-measurement

**Latency waterfall**: already measured and documented in
`docs/PERFORMANCE_BASELINE.md` — reproduced as the canonical reference:

```
generate_sql (LLM)         93.8% of wall-clock time   (mean 63.2s)
plan_query (LLM, gated)     3.6%                       (mean 2.4s, P50 0ms — zero-cost for simple questions)
retrieve_schema (Chroma)    1.3%                       (mean 0.9s)
generate_insight (LLM)      0.6%                       (mean 0.5s)
retrieve_golden_examples    0.4%                       (mean 0.3s)
execute_sql (live DB)       0.1%                       (mean 65ms)
estimate_query_cost         0.1%                       (mean 56ms)
validate_sql                0.0%                       (mean 16ms)
sanitize_input / classify_followup / review_sql (gated)  ~0%
```

**LLM call inventory** (Part 4's requested table, populated from this
session's own fresh verification — see the discovery pass this document
is built from):

| Call | Purpose | Required? | Cached? | Parallelizable? | Model |
|---|---|---|---|---|---|
| `generate_sql` | Core SQL generation | Always (≥1x, up to `max_retries`+bonus) | No | No — inherently sequential, each retry needs the previous error | `llama3.1:8b` (only model configured) |
| `plan_query` | Decomposition plan | Only if complexity signals fire | No | No (feeds `generate_sql`'s prompt) | same |
| `review_sql` | Plan-conformance check | Only if a plan exists | No | No (gates whether to retry) | same |
| `generate_insight` | Result summary sentence | Only if `enable_insight=True` and execution succeeded | No | Could run parallel to nothing (it's the last step) | same |
| `classify_sources` (router) | Multi-source routing | Only if ≥2 sources configured AND `ENABLE_MULTI_SOURCE_ROUTER=true` | No | N/A — happens once, upstream of fan-out | same |
| `web_search_node` synthesis | Draft answer from search hits | Only if "web" routed | No | Could run parallel to other routed sources (LangGraph already fans out subgraphs in one step per `CLAUDE.md`'s "Multi-source orchestration" section — confirmed already parallel) | same |
| `document_rag`/`policy_rag` generation | Cited answer from chunks | Only if routed | No | Same as above — already parallel with other sources | same |
| Voice correction (optional) | Clean up STT transcript | Only if voice mode + `enable_voice_correction` | No | N/A, off critical path | same |

**No response-level LLM caching exists anywhere on the backend** — every
`/ask` re-runs full generation even for a byte-identical repeated question.
The only response cache is client-side (`chatStore.ts`'s `nlQuestionCache`,
session-scoped, one browser tab). This is a real, low-risk P1 opportunity
(§7) — but note it doesn't reduce the *tail*, since a cache miss (the
common case for genuinely distinct questions) pays the same LLM cost; it
only helps the identical-repeat case (typing the same question twice, or
a follow-up in a shared demo).

**Conclusion, unchanged from `docs/PERFORMANCE_BASELINE.md` and
`docs/PERFORMANCE_RESULTS.md`**: this application's own code is not the
bottleneck. 93.8-98% of latency is one or more sequential LLM calls to a
single local `llama3.1:8b` instance. The only three levers that would
measurably reduce it (smaller/faster model, GPU hardware, smaller prompt)
are each an explicit AI-quality or infrastructure tradeoff, already
identified and deliberately not taken without separate approval. **This
document does not re-litigate that conclusion** — the evidence is real,
recent, and honestly reported (including a regression that was reported
as a regression, not hidden).

**What genuinely is new and actionable, not covered by the Phase 1-3
pass**: *perceived* latency (streaming — Part 21), and *aggregate*
observability (the timing data exists per-request in logs, but there is no
queryable rollup across requests — Part 26). See §5 and §7.

## 4. Security Assessment (synthesis)

Unchanged from `docs/PHASE1_BASELINE.md` §6 and
`docs/PLATFORM_TRANSFORMATION_ASSESSMENT.md` §4 — both independently
verified this session via the discovery pass (AST-based SQL validator,
untrusted-data framing in every relevant prompt, SSRF IP-resolution
checks, magic-byte upload validation, `SecretStr` everywhere, blocking CI
gates). **No new vulnerability found in this pass.** The disclosed,
still-open items (no circuit breakers, in-memory-only rate limiting, DNS-
rebinding TOCTOU window in the SSRF check, PII-in-logs for question
text/SQL, 3 production-readiness verification gaps) are unchanged and not
re-litigated here.

**One new-to-this-pass security-adjacent finding**: `agent/tools/`'s
`ToolRegistry.execute` already does real permission checks
(`agent.authz.has_role_permission`) independent of the orchestrator's own
`router_node` check — meaning **if** it's ever wired in as the actual
dispatch path, permission enforcement would exist in two independently-
implemented places (the platform assessment's own `TOOLS.md` already flags
this as the exact reason wiring was deferred, "permission-checked twice,
in two different ways, with real drift risk"). Any future integration
must pick exactly one enforcement point, not layer both.

## 5. Context/Token Management, Streaming, Model Routing, Caching — new findings

**Context engineering (Part 11)**: already minimal by construction, not by
active compaction. `_build_user_prompt` never includes more than the
single most-recent conversation exchange — every earlier turn is dropped
before reaching a prompt, not summarized. This actually satisfies the
master prompt's underlying goal ("do not send the entire conversation to
every model call") more strongly than a summarization layer would, at the
cost of not supporting genuinely multi-turn reasoning beyond one prior
exchange. **Recommendation: do not build a context-compaction layer for a
problem that doesn't exist yet** — this would be solving for a context
window this application never fills. Revisit only if/when Deep Research
Mode (§7) needs to accumulate multi-step evidence across a longer working
context.

**Memory (Part 12)**: no separate conversation/project/user/tool-cache/
research-memory distinction exists — the closest thing is `chatStore`'s
`nlQuestionCache` (a flat, unscoped question→answer map) and the schema-
embedding fingerprint cache (a tool/schema cache, correctly scoped per
database). Building a full memory taxonomy now would be premature — there
is no product surface yet (no Projects/Workspaces) for user/project memory
to attach to. **P2, blocked on the Workspace data model.**

**Streaming (Part 21)**: confirmed missing end-to-end (backend never
passes `stream=True` to Ollama; `/ask` returns one JSON body; frontend has
no SSE/streaming consumer). This is real, user-visible latency-perception
debt given §3's finding that a single question can take 30-180+ seconds —
a static "Thinking…" spinner for that long is a materially worse
experience than progressive tokens or even progressive *stage* updates
("Retrieving schema… Generating SQL… Validating…"). **P1** — see §7 for a
staged proposal (stage-level progress before token-level streaming, since
stage progress is nearly free given the timing instrumentation already
exists, while token streaming requires reworking the synchronous
thread+`.join()` request handling in `api/main.py`).

**Model routing (Part 6)**: confirmed hardcoded to one provider (Ollama)
and one model (`llama3.1:8b`) for every call type — SQL generation,
planning, review, insight, routing classification, RAG generation, and
voice correction all share the identical model. No abstraction exists to
route a cheap classification call (e.g., `classify_sources`, `review_sql`)
to a smaller/faster model while reserving the full model for SQL
generation itself. **This is a real, measurable potential win**
(`plan_query`/`review_sql`/`classify_sources` are all "is this true/which
category" style calls that a much smaller model could plausibly handle at
a fraction of the latency) — but per `docs/PERFORMANCE_RESULTS.md`'s own
explicit reasoning, this is an AI-quality tradeoff requiring separate
evaluation-backed approval (a smaller classifier model could silently hurt
`review_sql`'s plan-conformance catch rate), not a blind infrastructure
change. **P1, gated on an evaluation harness extension** (§7's RAG/web
eval item establishes the pattern needed to safely measure this too).

**Caching (Part 22)**: full inventory in this session's own discovery pass
(§ "Caching Inventory" in the underlying research) — every existing cache
is either a process-lifetime singleton (connection pools, compiled graphs,
client objects — correct and complete) or the schema-embedding fingerprint
cache (correct and complete). **No response-level cache exists for
RAG/web-search results** (a repeated identical RAG question re-embeds the
query and re-searches Chroma every time; a repeated identical web search
re-calls Tavily every time) — both are real, safe candidates for a
short-TTL cache (RAG: invalidate on document re-ingestion; web: TTL-based,
since freshness matters). **P1**, see §7.

## 6. MCP / Tool Registry — the single highest-leverage next step

Re-confirmed this session, independent of `docs/TOOLS.md`'s own claim:
`agent/tools/registry.py`/`definitions.py` are fully built, permission-
checked, timeout-bounded, retry-policy-governed, audit-logged, and tested
(27 tests) — and **zero production code paths call them**. This is the
one item in this entire assessment that requires **no new design work**,
only a wiring decision. Two real options, not yet chosen:

1. **Wire it in behind the existing orchestrator nodes, unconditionally**
   (each node body calls `ToolRegistry.execute` instead of its target
   module directly) — low product value alone (nothing changes
   user-visibly), but closes the "authored, never exercised in production"
   gap and gives every source call uniform timeout/retry/audit behavior
   for free.
2. **Make it the dispatch layer for a genuinely new consumer** — the
   Research Planner upgrade (Part 1/13) is the natural first real
   consumer, since a multi-step planner choosing tools dynamically by name
   is exactly what a generic registry is for, whereas the orchestrator's
   fixed-graph-edge routing (option 1) doesn't actually need dynamic
   dispatch.

**This document's recommendation, reconciling both prior roadmaps**:
option 2, deferred until the Research Planner is actually being built —
wiring it into the orchestrator today (option 1) would touch a
"~1,150-line, heavily security-reviewed module" (per `TOOLS.md`'s own
words) for zero behavioral benefit, the same reasoning that deferred it
last time. Not re-litigating that decision; it still holds.

## 7. Reconciled Target Architecture (Part 33)

`docs/PLATFORM_TRANSFORMATION_ASSESSMENT.md` §6's target diagram already
correctly identifies the shape (existing router → upgrade to a real
planner; existing parallel source fan-out, kept; new Artifact/Workspace
layer). This document adds three refinements specific to this prompt's
performance/observability focus, layered onto the same diagram without
changing its shape:

```
                         USER
                           |
                           v
                 React dashboard / REST API        (existing, unchanged)
                           |
                           v
                  [NEW: lightweight metrics/observability layer —
                   aggregates the ALREADY-LOGGED stage_timings into a
                   queryable rollup; zero new instrumentation needed,
                   only aggregation of what agent/nodes.py already emits]
                           |
                           v
        router_node (existing) --> upgrade to a REAL multi-step planner
                           |         (new: ResearchPlan object; the natural
                           |          first real consumer of agent/tools/
                           |          ToolRegistry for dynamic dispatch)
                           v
          +----------------+----------------+----------------+
          |                |                |                |
          v                v                v                v
   sql_subgraph      document_rag/     web_search      media_search/
   (existing,         policy_rag        (existing,       generation
    unmodified)       (existing,         + NEW: short-    (existing,
                       unmodified)        TTL result       unmodified)
                                          cache)
          |                |                |                |
          +----------------+----------------+----------------+
                           |
                           v
                  synthesis_node (existing)
                           |
              +------------+------------+------------+
              |            |            |             |
              v            v            v             v
           ANSWER        CHART      ARTIFACT       WORKSPACE
      (existing; NEW:  (upgrade:    (NEW, depends   (NEW: Project/
       stage-progress   real type    on Workspace)   SavedQuery/
       events before    coverage)                    SavedReport)
       token streaming)
```

**Deliberately not changed**: the two-graph LangGraph shape, the
AST-based SQL validator, the fail-open/fail-closed posture, the
single-Ollama-provider deployment model (model routing is a P1 evaluation-
gated change, not a rip-and-replace of the provider abstraction). Per the
prompt's own rule 36 ("do NOT rewrite the entire application
unnecessarily... do NOT replace LangGraph without evidence"), nothing
above requires touching `agent/graph.py`'s existing 12 SQL-pipeline nodes.

## 8. What Cannot Be Honestly Measured In This Session

Stated explicitly per the prompt's own "do not claim scalability without
measurements" / "do not fabricate test results" rules:

- **10/50/100-concurrent-user load testing**: would require either a
  dedicated, isolated benchmark environment (not this shared development
  machine — see `docs/PERFORMANCE_RESULTS.md`'s own finding that even
  *sequential* runs on this machine show 7x latency swings from
  contention) or a real staging deployment. **P2 — infrastructure/
  environment item, not a code gap.**
- **DAST, live-IdP OIDC, real-ClamAV verification**: already identified as
  the three concrete production-readiness blockers by
  `docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`; unchanged,
  still **P2 — operational, not code.**
- **RAG/web-search evaluation numbers**: no eval harness exists yet for
  either (§1's finding) — a P1 *build* item before any numbers can exist
  to report.

## 9. Reconciled P0 / P1 / P2 Roadmap

Merges `docs/PLATFORM_TRANSFORMATION_ASSESSMENT.md` §10 with this
document's performance/observability findings. Items already completed
are marked so the roadmap doesn't re-propose finished work.

**P0 — foundational, low-risk, additive, unlocks measurement for
everything else — ALL CLOSED (2026-09-19)**
1. ~~Generic tool/MCP abstraction~~ — **already done** (`agent/tools/`)
2. ~~Observability rollup~~ — **done**: `observability/metrics.py` +
   `GET /metrics/performance` (admin-only), wired into
   `agent.graph.run_agent`, 11 tests. See `CLAUDE.md`'s "Observability —
   live performance rollup" section.
3. ~~Doc-drift fixes~~ — **done**: the 11-vs-12-node count fixed in
   `CLAUDE.md`. (The `rag`/`search` test-coverage claim the platform
   assessment flagged turned out to already be corrected — verified, not
   re-fixed.)
4. ~~`search/web_search.py` unit tests + the `mypy .` module-resolution
   fix~~ — **done**: 14 new tests (`tests/test_web_search.py`);
   `scripts/__init__.py` added, closing the crash. That unmasked 117
   pre-existing type errors; 12 were fixed in the same pass (6 in this
   pass's own new test files, 6 in production code —
   `media/ingest.py`'s stale `object`-typed field,
   `moderation/gate.py`'s `Literal` narrowing, a `build_media_index.py`
   variable-naming false-positive). **~105 remain, all in untouched test
   files — a real, separate, bounded mypy-hygiene pass, not attempted
   here.** See `CLAUDE.md`'s updated "Known, pre-existing CI-hygiene
   gaps" note for the full breakdown.

**P1 — genuinely new capability, each independently justified**
5. Research Planner upgrade (one-shot classifier → real, observable,
   step-by-step `ResearchPlan` object) — depends on P0.2 for progress
   visibility, is the natural first live consumer of `agent/tools/`
   (§6, option 2). **Not started** — deliberately deferred; this is the
   single largest, most architecturally-sensitive item on this list (see
   §6's own reasoning for why it shouldn't be rushed into the existing
   orchestrator).
6. Stage-level progress streaming (Part 21) — cheap given existing timing
   instrumentation; token-level streaming is a larger, separate change to
   `/ask`'s synchronous request handling, evaluate only after stage-level
   progress ships and is validated. **Not started.**
7. ~~RAG-specific and web-search-specific evaluation harnesses~~ —
   **done**: `eval/rag_evaluators.py` (retrieval recall/precision,
   citation correctness against actually-retrieved chunks, citation
   coverage) and `eval/web_search_evaluators.py` (citation-fabrication
   detection — parses the model's own inline markdown citations and
   verifies every URL was a real search result, catching exactly the kind
   of fabrication a naive recall/precision check against the
   already-complete structured citations list couldn't). 42 tests. **Not
   yet wired into a live runner/dataset/reporting pipeline** (mirroring
   `eval/runner.py`'s fuller SQL integration) — the grading logic is real
   and tested; driving it against live `rag.graph.run_rag`/
   `search.web_search.web_search` calls with a real benchmark dataset is
   a separate, still-open follow-up.
8. Model-tier routing for classification-shaped calls
   (`classify_sources`/`review_sql`/`plan_query`) — gated on P1.7's
   evaluation harness so any model swap is measured, not assumed safe.
   **Not started** (the harness now exists; the routing change itself
   doesn't yet).
9. Short-TTL result caching for RAG and web search (not SQL — SQL results
   must reflect live data). **Not started.**
10. Workspace/Project/SavedQuery/SavedReport data model + API (already
    scoped by the platform assessment). **Not started** — new DB
    migrations + RBAC surface, deliberately out of scope for this pass.
11. Interactive Artifacts (depends on P1.10). **Not started.**
12. ~~AI Data Analyst depth~~ — **done**: `agent/insight.py`'s
    `ResultSummary` gained deterministic trend (period-over-period
    change), variance (population stddev + coefficient of variation), and
    outlier detection (>2 stddev from the mean, 3+ labels required), all
    flowing through the existing grounding gate. 16 new tests. **Not yet
    wired into `_build_insight_prompt`** — deliberately deferred, since
    this session had no way to re-run the 57-case live-Ollama benchmark
    `docs/EVALUATION_CURRENT.md`'s numbers depend on to confirm a prompt
    change doesn't shift generation behavior. See `CLAUDE.md`'s "AI Data
    Analyst depth" section for the full reasoning.
13. Frontend chart-type coverage matching backend intent. **Not started.**

**P2 — operational verification, polish, or explicitly deferred
infrastructure decisions**
14. Real load testing, in a dedicated environment (§8)
15. DAST / live-IdP OIDC / real-ClamAV verification (§8, unchanged from
    prior reports)
16. Circuit breakers around Ollama/Azure Content Safety/Tavily/IMA
    (`docs/RISK_REGISTER.md` R-006, still open)
17. Redis-backed (multi-process-safe) rate limiting, if/when this ever
    moves to a multi-worker deployment
18. Memory (user/project) — blocked on P1.10's Workspace model existing
    first
19. Citation richness for web/SQL sources to match RAG's already-good
    structured-`Citation` shape

**Recommended dependency order** (unchanged reasoning from the platform
assessment, extended): P0 items are independent and can run in any order
or in parallel — none touches security-sensitive or heavily-coupled code.
P1.5 (Research Planner) is the pivot point most other P1 items either
depend on (P1.6's progress events need something to report progress
*about*) or benefit from (P1.8's model routing is most valuable once a
planner is actually choosing between more call types).

**Per the master prompt's own instruction not to build all of this in one
session** — this is genuinely multiple weeks of work spanning
observability infrastructure, a new planner abstraction, two new
evaluation harnesses, and a new data model. Proceeding now with **P0.2
(observability rollup)** as the first concrete implementation step: it is
the most foundational (every later "is this faster/better" claim needs it
to be evidence-based), the lowest-risk (purely additive, reads existing
log data, touches no request-path behavior), and was independently
identified by both this document and the original master prompt's own
"Phase 0 — Foundation" list as the correct starting point.
