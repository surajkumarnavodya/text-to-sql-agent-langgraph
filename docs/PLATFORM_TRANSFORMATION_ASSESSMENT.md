# Enterprise AI Intelligence Platform — Current-State Assessment & Roadmap

**Date:** 2026-09-18
**Scope:** Response to `claude_ai_intelligence_platform_master_prompt.md` Parts 1–19 — codebase discovery and
assessment only (no implementation in this pass). Every claim below is grounded in a real file path/line,
verified by opening the actual source in this session (not taken from `CLAUDE.md` prose uncritically — three
research passes independently cross-checked `CLAUDE.md`'s own claims against live code and flagged the two
places it's stale; see §9).

---

## 1. Executive Summary

**What this application currently does.** A user asks a natural-language question; a 12-node LangGraph state
machine (`agent/graph.py`) turns it into validated, read-only SQL against one or more configured databases
(PostgreSQL/MySQL/SQL Server/Oracle), executes it, and returns a table + chart + optional grounded insight. On
top of that core pipeline sits an **8-node orchestrator graph** (`agent/orchestrator/graph.py`) that — when
`ENABLE_MULTI_SOURCE_ROUTER=true` — classifies a question and fans it out in parallel to any combination of:
SQL, uploaded-document RAG, a separately-sensitivity-gated policy RAG collection, live web search (Tavily),
content-based media search (local CLIP), and image/video generation (IMA Studio, human-approved before spend).

**This is already substantially the "multi-source agentic platform" the master prompt describes as the target
vision**, not a green-field system to be built. The gap between current state and the master prompt's Part 4
feature list is real but narrower than the prompt assumes — see §7.

**Current architectural pattern.** Explicit LangGraph state machines (two of them: SQL pipeline + orchestrator),
not a ReAct-style free-form agent, not a generic tool-calling framework. Every "source" is a hardcoded graph node
calling its own module directly (`sql_subgraph_node` → `agent.graph.run_agent`, `web_search_node` →
`search.web_search.web_search`, etc.) — deliberate, for inspectability, but it means **there is no generic
MCP/tool abstraction today** (confirmed: zero matches for `BaseTool`/`ToolRegistry`/`input_schema` patterns
anywhere in the repo).

**Current AI/agent capabilities.** Schema-scoped retrieval (ChromaDB, top-k + FK-bridge expansion), a
golden-example few-shot feedback loop, business-context RAG (glossary/metrics/SQL examples), adaptive
retry budgets with plan-conformance self-correction for complex queries, and a one-shot LLM classifier (not a
multi-step research planner) for source routing.

**Current frontend capabilities.** React + Vite + TS + Tailwind, PWA-installable, 4 routes (chat, knowledge
sources, media search, OIDC callback), voice mode, per-source citation panel, CSV/PDF export of individual
answers — but the composer itself is just a textarea + mic + send (no attach-file, no source picker, no
research-mode toggle), and chart rendering client-side is limited to bar/line regardless of what the backend
intends.

**Current security posture.** Materially mature: OIDC (PKCE, server-side alg allowlist, audience/issuer
validated) + local accounts + 4-role/15-permission RBAC + AST-based SQL validator (blocks writes at the node-type
level, not regex) + mandatory content-moderation gate for RAG/media ingestion + opt-in malware scanning (real
ClamAV INSTREAM protocol) + blocking CI security gates (bandit/pip-audit/detect-secrets/mypy/ruff/black/pytest —
verified in the actual `ci.yml`, not just claimed) + audit logging with correlation IDs + Docker non-root/pinned/
capability-dropped. **1,406 tests collected across 90 files, all green-path collectible with zero errors** (verified
this session via `pytest --collect-only`).

**Current production readiness.** The project's own `docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`
verdict is **NOT READY**, for three specific, honestly-disclosed reasons — none is "a vulnerability was found":
DAST has never been run, OIDC has never been exercised against a live IdP, and the malware scanner has never
been tested against a real ClamAV daemon (only mocked). This session's audit found no overstatement anywhere in
that report — if anything the docs under-claim relative to what's implemented.

**Major technical strengths.**
- AST-based (sqlglot) SQL validator, not a blocklist — genuinely hard to bypass with a syntax variant.
- Explicit, bounded, inspectable state machines instead of implicit agent loops — retries capped via
  `recursion_limit = 20 + max_retries*10` (`agent/graph.py:372`), not unbounded.
- Fail-open/fail-closed posture is applied *consciously and consistently* per subsystem (accuracy aids fail
  open; safety checks fail closed) — a real, disciplined design principle, not accidental.
- Security documentation is unusually honest — every "NOT READY"/"NOT VERIFIED"/"PARTIAL" finding is backed by
  a specific missing piece of evidence, not a vague caveat.

**Major technical weaknesses (verified gaps, not assumptions).**
- No generic tool-calling/MCP framework — adding a new source means writing a new hardcoded node, not
  registering a tool.
- No multi-step "research plan" object or progress UI — the router is one Ollama call picking a source *set*,
  not an iterative, observable research workflow (Master Prompt Parts 1–2 are genuinely not built yet).
- No Workspace/Project/SavedQuery/SavedReport/Artifact data model — `identity/models.py` stops at
  `Conversation`/`Prompt`/`AiOutput`. Everything that isn't a chat turn is ephemeral, client-side-only.
- "AI Data Analyst" (`agent/insight.py`) is a single grounded sentence (min/max/sum/top-share) — no
  trend/variance/correlation/anomaly/forecasting logic exists.
- Rate limiting is honestly disclosed as in-memory-only — won't hold under a horizontally-scaled deployment.
- Frontend chart rendering only distinguishes bar vs. line client-side, regardless of backend intent.
- `search/web_search.py` still has no dedicated unit test file (rag/ did get one since `CLAUDE.md` was last
  updated — see §9's doc-drift finding).

---

## 2. Repository Structure

```
agent/                  LangGraph SQL pipeline (12 nodes) + orchestrator subgraph (8 nodes)
  graph.py                sanitize→classify_followup→retrieve_schema→retrieve_golden_examples→
                           retrieve_business_context→plan_query→generate_sql→review_sql→validate_sql→
                           estimate_cost→execute_sql→generate_insight
  orchestrator/            router→{sql_subgraph,document_rag,policy_rag,web_search,generation,
                           media_search} (parallel fan-out)→synthesis
  sql_validator.py         AST allowlist (sqlglot) — the core "SQL is untrusted output" boundary
  insight.py                single grounded-sentence result summarizer (not a statistical analyst)
  rate_limit.py             in-memory sliding-window + per-session expensive-source cost ceiling
  authz.py                  15-permission / 4-role RBAC map
api/                     FastAPI surface — /ask, /execute, /documents, /media, /generate, /voice, /search,
                         /conversations (chat history), /auth — correlation-ID middleware, global exception
                         handlers, lifespan-warmed singletons
db/                      SQLAlchemy engine lifecycle, schema introspection, read-only execution + row caps
embeddings/              Schema-DDL ChromaDB indexing/retrieval + golden-examples store
retrieval/               Business-context vector retrieval (glossary/metric/SQL-example/doc chunks)
rag/                     Document + policy agentic RAG subgraph (SQL Server VECTOR storage)
search/                  Web search provider abstraction (Tavily implemented) — no dedicated tests yet
media/                   Content-based image/video search (local CLIP + Chroma)
media_gen/               Image/video generation (IMA Studio), SSRF-hardened download, approval gate
voice/                   Local STT (faster-whisper) / TTS (Piper)
moderation/              Mandatory content-moderation gate (Azure AI Content Safety) for RAG/media ingestion
security/                OIDC, audit logging, malware scanner (real ClamAV INSTREAM), redaction
identity/                Self-hosted accounts (Argon2id+JWT), RBAC bridge, chat history (Conversation/
                         Prompt/AiOutput) — 14 tables total, no Workspace/Project/Artifact entities
config/                  Pydantic-settings config bag + YAML-driven classification files
eval/                    Text-to-SQL execution-accuracy benchmark harness
frontend/src/
  pages/                  Chat.tsx, KnowledgeSources.tsx, MediaSearch.tsx, AuthCallback.tsx — 4 routes total
  store/                  chatStore (flat conversation map), settingsStore, authStore, localAuthStore
  components/chat/        ChatInput (textarea+mic+send only), TurnCard, SourcesUsedPanel, SourceAnswerCard
  lib/                    chartAdapter (bar/line only), csv.ts, pdf.tsx (per-turn export)
tests/                   90 files, 1,406 tests collected cleanly
docs/                    ~35 markdown files + docs/security/ subfolder (unusually thorough)
```

---

## 3. Existing Feature Inventory

| Feature | Status | Relevant Files | Quality | Security | Recommendation |
|---|---|---|---|---|---|
| Text-to-SQL | **Existing, mature** | `agent/graph.py`, `agent/sql_validator.py`, `db/` | High — AST allowlist, cost estimation, retry loop with plan-conformance review | Strong — write statements blocked at node-type level anywhere in tree, not just root | Retain as-is; core differentiator |
| Multi-database intelligence | **Existing, mature** | `db/connection.py`, `embeddings/retriever.py::select_database` | High — 4 DB types, auto-routing, per-DB Chroma collections | Strong | Retain; extend DB type list only if a real customer needs it |
| LangGraph orchestration | **Existing, mature** | `agent/graph.py`, `agent/orchestrator/graph.py` | High — two purpose-built graphs, singleton-cached, recursion-capped | N/A | Retain; do not replace with a generic ReAct agent |
| Multi-source router | **Existing, partial vs. master prompt's vision** | `agent/orchestrator/nodes.py::router_node`/`classify_sources` | One-shot classifier, not an iterative research planner | Permission-filtered before any subgraph runs | **Gap**: needs a real execution-plan object + progress observability (Master Prompt Part 1–2) |
| Deep Research Mode | **Missing** | — | — | — | **Build**: multi-round search, evidence correlation, fact verification, progress UI — none of this exists today |
| Enterprise RAG (documents/policies) | **Existing, mature** | `rag/`, now with dedicated tests (`test_rag_graph.py`, `test_rag_ingestion.py`, `test_rag_pdf_download.py`) | High — chunking, moderation-gated ingestion, malware-scanned, RBAC + sensitivity-category gating | Strong | Retain; citation UI could show date/snippet (currently filename-only) |
| Web search | **Existing, partial** | `search/web_search.py` (Tavily only) | Functional but no dedicated unit tests, single provider | Untrusted-data framing in prompts, but no confirmed domain allow/blocklist enforcement found | Add unit tests; verify/implement domain allow/blocklist if required |
| Media search | **Existing** | `media/` (CLIP + Chroma) | Functional, off by default, real dependency cost (`torch`, OpenCV) | SSRF/path-traversal hardened | Retain |
| MCP/Tools | **Missing** | — | — | — | **Build**: no generic tool abstraction exists; every source is a hardcoded node |
| Authentication | **Existing, mature** | `security/oidc.py`, `identity/`, `api/auth.py` | High — real alg allowlist, PKCE, Argon2id | Strong, but OIDC never tested against a live IdP (disclosed) | Close the live-IdP verification gap before claiming production-ready |
| Authorization (RBAC) | **Existing, mature** | `agent/authz.py` | 15 permissions × 4 roles, fail-closed on unknown role | Strong | Retain |
| Security (broad) | **Existing, mature** | `security/`, CI `ci.yml`, `moderation/` | Blocking CI gates confirmed in real YAML; audit logging with correlation IDs | Strong, with 3 disclosed P0 verification gaps (DAST, live OIDC, real ClamAV) | Close verification gaps; do not add new controls before closing existing ones |
| Data Analyst / Visualization | **Existing, basic** | `agent/insight.py`, `frontend/src/lib/chartAdapter.ts` | Insight = one grounded sentence; frontend chart = bar/line only | N/A | **Gap**: no trend/variance/correlation/anomaly/forecast; chart adapter needs real type coverage |
| Workspace / Memory | **Existing, partial** | `identity/models.py` (Conversation/Prompt/AiOutput only) | Chat history is real and persistent for local-auth users; no Workspace/Project/SavedQuery/Artifact | N/A | **Build**: workspace/project grouping + saved-query/report entities |
| Interactive Artifacts | **Missing** (as a persistence system) | `frontend/src/lib/pdf.tsx`, `src/lib/csv.ts` | One-shot client-side export only, non-persistent, non-editable | N/A | **Build**: server-side artifact entity with view/edit/regenerate/export/share |
| Voice mode | **Existing, mature** | `voice/` | Fully local (faster-whisper + Piper), live captions disclosed as the one cloud exception | N/A | Retain |

*(Do not read "Missing" above as "aspirational fiction never attempted" — these are the specific, verified gaps
against the master prompt's Part 4 target list. Everything else in Part 4 is already built to a meaningfully
production-grade standard.)*

---

## 4. Security Assessment

Verified this session, not copied from docs:

- **SQL injection**: AST-level allowlist (`exp.Select/Union/Except/Intersect` only at root;
  `Insert/Update/Delete/Merge/Drop/Create/Alter/TruncateTable/Command` blocked **anywhere in the parsed tree**,
  catching write-shaped CTEs). Genuinely hard to bypass with a syntax trick.
- **Prompt injection**: input sanitization pre-filter (`sanitize_input` node) + retrieved content (RAG chunks,
  web results, golden examples) explicitly framed as untrusted data, never instructions, in every relevant
  system prompt.
- **SSRF**: media download path re-validates every redirect hop (up to 5), not just the initial URL.
- **File upload**: extension + MIME + size validation, mandatory moderation gate (fails closed if
  misconfigured), opt-in malware scanning (fails closed once enabled).
- **Authn/Authz**: OIDC with real algorithm allowlist and audience/issuer validation; RBAC fail-closed on
  unrecognized roles; local accounts use Argon2id + rotating refresh tokens with reuse-detection.
- **Rate limiting**: real but explicitly in-memory/single-process — will not hold under horizontal scaling.
  This is disclosed, not hidden, but is a genuine architectural limitation if this app is ever deployed with
  multiple workers/replicas.
- **Secrets management**: no hardcoded secrets found repo-wide (regex + pattern sweep); `.env` gitignored and
  untracked; `.env.example` contains only placeholders.
- **CI security gates**: detect-secrets/ruff/black/mypy/bandit/pytest/pip-audit are genuinely blocking in the
  real `ci.yml` (verified line-by-line); gitleaks and Trivy remain `continue-on-error: true`, each with a dated
  justification comment — an honest, not silent, exception.
- **Container**: non-root user, digest-pinned base images, `cap_drop: [ALL]`, `no-new-privileges`, resource
  limits, compose binds to `127.0.0.1` by default.
- **Known, disclosed gaps**: no circuit breakers anywhere; DAST never run; OIDC never tested against a live
  IdP; malware scanner never tested against a real ClamAV daemon; `mypy .` currently fails on a pre-existing
  module-resolution error unrelated to any recent change (confirmed via `git stash` per `CLAUDE.md`).

---

## 5. Production-Readiness Assessment

**Verdict, unchanged from the project's own latest audit and independently spot-checked this session: NOT
READY.** The blockers are specifically verification gaps, not known vulnerabilities:

1. DAST has never been executed against this application in any environment.
2. The frontend OIDC Authorization Code + PKCE flow has never been exercised against a live identity provider
   (only mocked/unit-tested).
3. The malware scanner (`security/malware_scanner.py`) has real ClamAV INSTREAM protocol code, but has never
   been tested against an actual `clamd` daemon — 18 tests exist, all against a mocked socket.

Additionally, from this session's own checks: rate limiting is process-local (a real scaling limitation, not
just an unverified one), and `mypy .` fails to complete a full run due to a pre-existing module-resolution
conflict — both worth fixing before any "production-ready" claim, independent of the three P0s above.

**No new vulnerability was found in this assessment.** The tested security architecture (SQL validation, RBAC,
SSRF hardening, secrets hygiene, CI gates) holds up under direct code inspection.

---

## 6. Target Architecture (Adjusted)

The master prompt's target architecture diagram (Intent Classifier → Research Planner → {Database, Knowledge,
Web} Agents → Tool Executor → Evidence Collector → Analyst → Synthesizer → Answer/Chart/Artifact) is **already
~70% built** as the orchestrator graph. The adjusted target keeps the existing two-graph shape and adds four
new layers rather than replacing anything:

```
                         USER
                           |
                           v
                 React dashboard / REST API        (existing, unchanged)
                           |
                           v
        router_node (existing) --> upgrade to a REAL multi-step planner
                           |         (new: ResearchPlan object, step-by-step,
                           |          exposed to the UI as progress, not just
                           |          a one-shot source-set decision)
                           v
          +----------------+----------------+----------------+
          |                |                |                |
          v                v                v                v
   sql_subgraph      document_rag/     web_search      media_search/
   (existing)         policy_rag        (existing)       generation
                       (existing)                         (existing)
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
        (existing)    (upgrade:    (NEW: server-   (NEW: Project/
                     real type     side, persistent, SavedQuery/
                     coverage,     editable, export- SavedReport
                     not just      able, shareable)   entities)
                     bar/line)
```

**New layer: generic tool abstraction (MCP-shaped).** Rather than a big-bang rewrite of every existing source
node, introduce a thin `Tool` protocol (`name`, `description`, `input_schema`, `output_schema`, `permission`,
`timeout`, `retry_policy`) that the *existing* node functions register against — this makes future tools
(APIs, MCP servers) pluggable without re-architecting `sql_subgraph_node`/`web_search_node`/etc., which stay as
they are.

---

## 7. Gap Analysis (Verified, Not Assumed)

| Master Prompt Ask | Reality | Gap Size |
|---|---|---|
| 1. Universal AI Research Agent | Router exists but is one-shot, not planning/retrying/cancellable/observable as a multi-step object | **Medium** — upgrade existing router, don't replace |
| 2. Deep Research Mode | Not built at all | **Large** — genuinely new feature |
| 3. Multi-Database Intelligence | Already mature | **None** — retain |
| 4. Enterprise RAG | Already mature | **Small** — richer citations (date/snippet), nothing structural |
| 5. Web Search + Citations | Provider abstraction exists, single provider, no dedicated tests | **Small-Medium** — tests + optional domain allow/blocklist |
| 6. AI Data Analyst & Visualization | Insight = 1 sentence; chart = bar/line only | **Medium** — real statistical layer + chart-type coverage |
| 7. Agentic Tools / MCP | Not built | **Large** — genuinely new abstraction layer |
| 8. Workspace + Memory | Chat history exists; Workspace/Project/SavedQuery/Artifact do not | **Medium-Large** — new data model + API + UI |
| 9. Enterprise Security & Governance | Already mature; 3 disclosed verification gaps remain | **Small** — these are ops/verification tasks, not code gaps |
| 10. Interactive AI Artifacts | Exports exist but are one-shot/non-persistent | **Medium-Large** — new persistence + UI |

---

## 8. Dependency Analysis

- **Artifacts (Part 10) depends on Workspace/Project data model (Part 8)** — an artifact needs somewhere
  durable to live; building artifacts before workspace entities means throwaway work.
- **Deep Research Mode (Part 2) depends on the Research Planner upgrade (Part 1)** — the planner's step object
  is what the research-progress UI renders; building the UI first has nothing to bind to.
- **MCP/Tool platform (Part 7) is independent** of the others and lowest-risk to start first — it wraps
  *existing* nodes without touching their internals, and every other new source (future APIs) benefits
  immediately.
- **AI Data Analyst upgrade (Part 6) is independent** — `agent/insight.py` is a leaf module; extending its
  statistics doesn't require any of the above.
- **Security verification gaps (DAST, live OIDC, real ClamAV) are operational, not code**, and can proceed in
  parallel with any of the above — they need environments (a staging IdP, a running ClamAV daemon, a DAST scan
  target), not new source code.

**Recommended dependency order:** Tool/MCP abstraction → Research Planner upgrade → Deep Research Mode UI →
Workspace/Project data model → Interactive Artifacts → AI Data Analyst depth → chart-type coverage → web-search
tests/hardening → security verification gaps (parallelizable throughout).

---

## 9. Documentation Drift Found (worth fixing regardless of feature work)

`CLAUDE.md` currently states *"`rag/` and `search/` have no dedicated `pytest` unit test files yet."* This is
now **half true**: `tests/test_rag_graph.py`, `tests/test_rag_ingestion.py`, and `tests/test_rag_pdf_download.py`
all exist and directly test `rag.graph`/`rag.store` — one of `test_rag_graph.py`'s own docstrings confirms it
was added specifically to close this gap after `CLAUDE.md`'s note was written. `search/web_search.py` still has
no dedicated test file — that half of the claim is still accurate. Per "do not leave documentation describing
functionality that no longer exists" (Part 15), this should be corrected in `CLAUDE.md`'s "Known gaps" section
as a small, low-risk documentation fix, independent of the roadmap below.

---

## 10. P0 / P1 / P2 Roadmap (Adjusted From the Master Prompt's Default Sequence)

The master prompt's default P0 list assumes most of Part 4 doesn't exist yet. Since multi-database
intelligence, enterprise RAG, web search, and security/governance are already mature, they move out of P0 and
into "maintain + close specific gaps." The adjusted roadmap reflects actual dependency order (§8) and effort:

**P0 — foundational, unlocks everything else**
1. Generic tool/MCP abstraction wrapping existing source nodes (unlocks future extensibility with minimal
   disruption to working code)
2. Research Planner upgrade — turn the one-shot classifier into a real, observable multi-step plan object
3. Fix `CLAUDE.md` documentation drift (§9) — trivial, but "don't leave stale docs" is an explicit rule
4. Close the two smallest security gaps: `search/` unit tests, `mypy .` module-resolution fix

**P1 — the two genuinely new user-facing capabilities**
5. Deep Research Mode UI (depends on P0.2)
6. Workspace/Project/SavedQuery/SavedReport data model + API
7. Interactive Artifacts (depends on P1.6)
8. AI Data Analyst depth (trend/variance/correlation/anomaly-detection hooks) — independent, can run parallel
9. Frontend chart-type coverage to match whatever the backend already intends to send

**P2 — operational verification + polish (not blocked on anything above)**
10. Run DAST against a real deployment
11. Exercise OIDC against a live IdP
12. Test the malware scanner against a real ClamAV daemon
13. Evaluate Redis-backed rate limiting if/when this moves to multi-worker deployment
14. Citation richness (date/snippet) in `SourceAnswerCard.tsx`
15. Accessibility audit pass (currently "partial" — native semantics present, ARIA sparse)

I am **not** proposing to build all of P0–P2 in this session — that is weeks of work across security-sensitive
surface area (auth, data model, CI). Per the master prompt's own instruction ("adjust priorities... do not
blindly follow this sequence... proceed incrementally according to the **agreed** dependency order"), the next
step is to confirm with you which P0 item to start on first.
