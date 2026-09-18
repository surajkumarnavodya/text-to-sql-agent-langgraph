# LangGraph Upgrade — Security Assessment (Migration NOT Performed)

**Status: assessment only.** Per this session's explicit instruction, the
`langgraph` 0.2→1.x migration was **not attempted**. This document exists
so that decision is informed, not just deferred silently — it's the
required output in place of the migration itself.

---

## 1. Current vs. fixed version

| Package | Installed | Latest available (checked 2026-09-18, `pip index versions`) | First version with all relevant CVEs fixed |
|---|---|---|---|
| `langgraph` | 0.2.62 | 1.2.11 | `>=1.0.0` |
| `langgraph-checkpoint` | 2.1.2 | 4.2.0 | `>=3.0.0` (RCE fixes), `>=4.0.0`/`4.1.1` (cache-layer RCE fix) |
| `langgraph-sdk` | 0.1.74 | 0.4.4 | `>=0.3.15` |
| `langchain-core` | 0.3.86 | 1.6.3 | `>=1.2.22` |

Worth noting explicitly: "1.0" undersells how far behind this pin actually
is — the latest `langgraph` release is **1.2.11**, a full further minor
generation past the `1.0.0` floor that fixes the known CVEs. A real
migration should target a current `1.2.x` release, not just the minimum
CVE-fixed floor, to avoid immediately being behind again.

## 2. Vulnerabilities this upgrade would fix

All cross-referenced from `docs/security/CVE_TRIAGE.md` §1 — see that
document for full per-CVE detail (severity, exploitability, evidence).
Every one of these is currently assessed **NOT REACHABLE** in this app's
actual deployment, independently re-verified this session:

| CVE | Package | Mechanism | Reachable today? |
|---|---|---|---|
| CVE-2026-28277 | `langgraph`/`langgraph-checkpoint` | Checkpoint msgpack deserialization RCE | No — no checkpointer configured |
| CVE-2025-64439 | `langgraph-checkpoint` | `JsonPlusSerializer` RCE (default checkpoint serializer) | No — same reason |
| CVE-2026-48775 | `langgraph-checkpoint` | `JsonPlusSerializer` RCE (checkpoint bytes modified at rest) | No — same reason |
| CVE-2026-27794 | `langgraph-checkpoint` | Node result-caching RCE (`BaseCache`/`CachePolicy`) | No — neither mechanism used |
| CVE-2026-48776 | `langgraph-sdk` | Unsafe URL path construction | No — `langgraph_sdk` never imported; this app never runs as a client of a remote LangGraph Platform server |
| CVE-2026-34070 | `langchain-core` | Arbitrary file read via `prompts.loading` | No — `agent/llm_client.py` builds every prompt as an f-string; never deserializes a LangChain prompt object |
| CVE-2026-26013 | `langchain-core` | SSRF via `ChatOpenAI` image-token counting | No — this app is Ollama-only, `ChatOpenAI`/`langchain_openai` never imported |

**Security impact of staying on 0.2.62 today: none currently measurable.**
Every fixed vulnerability requires a feature (a configured checkpointer, a
cache backend + `CachePolicy`, a `langgraph_sdk` client connection, a
LangChain prompt-file load, or a `ChatOpenAI` instantiation) this codebase
never turns on. Consistent with this project's own standing disclosure
elsewhere (`docs/DEPENDENCY_SECURITY.md`): **"not reachable today" is a
property of this app's current architecture, not a permanent guarantee** —
if a future change ever adds a checkpointer (e.g. for the long-standing,
separately-tracked "follow-up question resolution... not backed by a
LangGraph checkpointer" gap noted in `CLAUDE.md`'s own "Known gaps"
section) or node-level caching, this reachability analysis would need to
be redone *before* that feature ships, not after.

## 3. This app's actual LangGraph API surface (grounded, not assumed)

Migration complexity depends entirely on *which* LangGraph APIs a
consumer actually uses — a large, unfamiliar codebase would need to be
searched to find out; this one is small enough to fully enumerate.

**Exhaustive grep of every `langgraph`/`langchain_core` import in this
codebase** (`grep -rn "^from langgraph\|^import langgraph\|^from langchain_core\|^import langchain_core" agent`):

```
agent/graph.py:121:              from langgraph.graph import END, StateGraph
agent/orchestrator/graph.py:59:  from langgraph.graph import END, StateGraph
```

That's the **entire** direct LangGraph import surface of this application
— two files, one import line each, importing exactly two names. The full
set of `StateGraph`/`graph` API calls made anywhere in this codebase:

```
StateGraph(AgentState) / StateGraph(OrchestratorState)   -- construction
graph.add_node(name, func)                                -- ×20 across both graphs
graph.set_entry_point(name)                                -- ×2
graph.add_edge(a, b)                                       -- ×8
graph.add_conditional_edges(name, router_func, mapping)     -- ×7
graph.compile()                                             -- ×2, zero arguments
compiled_graph.invoke(state, config={"recursion_limit": n}) -- agent/graph.py
compiled_graph.invoke(state)                                 -- agent/orchestrator/graph.py
```

**Not used anywhere in this codebase**: `Send`, `Command`, `interrupt`/
`Interrupt`, `START` (this app uses the older `set_entry_point(...)` idiom
instead), any prebuilt agent (`create_react_agent`, `ToolNode`,
`langgraph.prebuilt`), any checkpointer (`MemorySaver`,
`SqliteSaver`/`PostgresSaver`, `BaseCheckpointSaver`), any caching
(`BaseCache`, `CachePolicy`), and no direct `langchain_core` import at all
(it's a transitive dependency of `langgraph` only).

**What this means for complexity, concretely:** the publicly-reported 0.2→1.0
breaking changes this session could find secondary-sourced reporting for
(not independently confirmed against official docs — see §4's caveat) —
`Interrupt` class restructuring (4 fields → 2), a Python 3.9→3.10 floor
bump, checkpoint-format incompatibility, and a reported `ToolNode`
unit-testing regression in `langgraph-prebuilt` 1.0.1 — touch **none** of
the APIs this app actually calls. This is a real, positive signal (a
narrow, conservative API surface is inherently lower-risk to move), but it
is not the same thing as "confirmed safe" — see §5.

## 4. Sourcing caveat (honesty, not confidence)

This session attempted to fetch LangGraph's own official migration guide
(`langchain-ai.github.io`/`docs.langchain.com`) directly — both attempted
URLs returned HTTP 404 in this environment (the docs appear to have moved
between the source being current and this session's access attempt). The
breaking-change summary in §3 above is therefore sourced from a general
web search returning third-party/community summaries (blog posts, GitHub
issue trackers), **not** LangChain's own release notes read directly.
Treat §3's "publicly-reported changes" list as directional context, not a
verified, exhaustive changelog — the recommended migration plan in §6
below starts with re-fetching and actually reading the official migration
guide as its literal first step, specifically because this session
couldn't.

## 5. Migration complexity assessment

**Overall: LOW-TO-MODERATE, conditionally** — lower than a typical
LangGraph application because of the narrow API surface in §3, but not
zero, for reasons specific to this codebase:

- **`langgraph`, `langgraph-checkpoint`, `langgraph-sdk`, and
  `langchain-core` all need to move together** — they're resolved as one
  dependency graph by `langgraph`'s own version constraints, not four
  independent upgrades. A partial bump (e.g. `langgraph` alone) is not a
  realistic path.
- **`set_entry_point(...)` is this app's one idiom that might not carry
  forward unchanged** — modern LangGraph documentation/examples
  consistently show `graph.add_edge(START, "node")` instead. Whether
  `set_entry_point` is still supported (deprecated-but-working) or removed
  in 1.x is exactly the kind of thing this session's failed doc-fetch
  attempts (§4) left unconfirmed — **first thing to verify** before
  writing any migration code.
- **`AgentState`/`OrchestratorState` are plain `TypedDict`s** (per
  `agent/state.py`/`agent/orchestrator/state.py`, not re-read in full this
  session but referenced throughout `CLAUDE.md`) — `StateGraph`'s generic
  state-type contract is one of LangGraph's most stable APIs across
  versions; this is a low-risk area.
- **63 released versions of drift** (`0.2.62` → `1.2.11`) is a lot of
  surface even if the *specific* APIs this app calls are stable ones —
  transitive behavior (retry/error semantics, recursion-limit enforcement,
  node execution ordering for `add_conditional_edges`'s fan-out, which
  `CLAUDE.md`'s own "Multi-source orchestration" section explicitly
  depends on for parallel branch execution in one graph step) needs to be
  **regression-tested against real behavior, not just "does it import."**
- **This app's own test suite (1,406 tests as of this session, all
  passing) is the single biggest asset for this migration** — most of
  `tests/test_agent_nodes.py`, `tests/test_orchestrator.py`,
  `tests/test_sql_agent_integration.py`, and `tests/test_complexity.py`
  exercise graph behavior (retry routing, conditional-edge destinations,
  parallel fan-out) at a level that would catch a real behavioral
  regression, not just an import error.
- **The live Text-to-SQL benchmark
  (`scripts/run_benchmark.py --check-regression` against
  `eval/baselines/latest.json`) is a real gap for this specific
  migration** — it requires a live Ollama instance and database neither
  available in this sandboxed environment nor wired to run automatically
  in CI (`benchmark-regression` job is `workflow_dispatch`-only, per
  `.github/workflows/ci.yml`'s own comment). A LangGraph upgrade that
  subtly changes retry/error-routing behavior could silently regress
  real-world accuracy in a way the mocked unit suite alone wouldn't catch
  — this is the same concern `docs/RISK_REGISTER.md`'s R-003 entry already
  raises about this app's Text-to-SQL accuracy generally, now specifically
  relevant to a graph-engine version bump.

## 6. Recommended migration plan (not started)

1. **Read LangGraph's actual current migration guide directly** (this
   session's own attempt failed — §4) before writing any code. Confirm in
   particular: is `set_entry_point` still supported in 1.2.x, and what
   (if anything) changed in `add_conditional_edges`'s fan-out semantics
   that `CLAUDE.md`'s "Multi-source orchestration" section depends on.
2. **Do this as its own dedicated branch/PR**, isolated from any other
   change — matches `docs/DEPENDENCY_SECURITY.md`'s own prior
   recommendation and this session's own instruction not to bundle it in.
3. **Bump all four packages together** (`langgraph`, `langgraph-checkpoint`,
   `langgraph-sdk`, `langchain-core`) to their current mutually-compatible
   versions, not just the CVE-fixed floor.
4. **Run the full mocked test suite** (`pytest`, currently 1,406 passing)
   and fix any import/API-shape breaks first — expected to be small given
   §3's narrow surface, but not assumed.
5. **Run `scripts/run_benchmark.py --check-regression` against a real
   Ollama + database instance** (not available in this sandboxed session)
   before merging — the one verification this environment structurally
   cannot provide.
6. **Re-run this exact reachability analysis (§2) against the *new*
   installed versions** before closing this out — a version bump can
   introduce new advisories as easily as it fixes old ones; don't assume
   the post-upgrade dependency tree is clean without re-scanning it
   (`pip-audit -r requirements.txt`).
7. **Update `docs/DEPENDENCY_SECURITY.md` and `docs/security/CVE_TRIAGE.md`**
   to reflect the fixed versions once complete, and close the
   corresponding rows in this document.

## 7. Compensating controls while this remains unmigrated

Already in place, none added by this document:

- No checkpointer is configured anywhere (`agent/graph.py`,
  `agent/orchestrator/graph.py`) — the precondition for 4 of the 7
  langgraph-family CVEs.
- No node-level caching (`CachePolicy`/`BaseCache`) is used — the
  precondition for the caching-layer RCE.
- This app never runs as a `langgraph_sdk` client against a remote
  LangGraph Platform server.
- This app never instantiates `ChatOpenAI` or loads a LangChain prompt
  object from a file — Ollama is the sole LLM backend
  (`agent/llm_client.py`).
- `pip-audit`'s CI gate (see `docs/security/CVE_TRIAGE.md`) explicitly
  allowlists these specific, individually-justified advisory IDs rather
  than silently ignoring the whole `langgraph` family — a genuinely new
  advisory in a *different* package, or a newly-reachable code path in
  one of these same packages, still fails the build.
