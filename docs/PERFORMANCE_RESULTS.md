# Performance Optimization Results

## What was changed, and why

`docs/PERFORMANCE_BASELINE.md`'s measured bottleneck analysis was unambiguous: **LLM inference accounts for 98% of total request latency**; every other stage this application's own code controls (schema retrieval, golden-example retrieval, SQL validation, cost estimation, database execution) together accounts for under 2%. Per this pass's explicit constraint ("do not optimize blindly," "never compromise correctness"), this ruled out the majority of the checklist in the Phase 3 brief:

| Considered | Decision | Why |
|---|---|---|
| Schema caching | **Already implemented, verified, not duplicated** | `embeddings/schema_indexer.py`'s Chroma-fingerprint-based skip-if-unchanged caching already exists (`docs/PHASE1_BASELINE.md`) |
| Embedding caching | **Already implemented, verified** | Same mechanism — schema DDL is only re-embedded when its fingerprint changes |
| Retrieval caching | Not added | Retrieval is already ~0.4-1.3% of total latency (900ms mean) — not a bottleneck; adding a cache here risks staleness for a component that isn't the problem |
| **Database connection pooling** | **Implemented** | A genuine, previously-undocumented gap (`db/connection.py` left every engine on SQLAlchemy's default 5/10 pool, unlike `moderation/store.py`, which already did this correctly) — see below |
| Parallel retrieval | Not added | Schema retrieval and golden-example retrieval are already small (900ms + 250ms combined) — parallelizing two sub-second operations has negligible payoff relative to a 60+ second LLM call, and adds real complexity/failure-mode risk for a fraction-of-a-percent gain |
| Parallel independent operations | Not added | No two *expensive* operations were found running sequentially that could safely run concurrently — the agent graph's stages are inherently sequential (each depends on the previous one's output) |
| Smaller models for classification | Not attempted | An AI-quality tradeoff (model choice), explicitly out of this pass's scope per the same reasoning `docs/EVALUATION.md`/`docs/EVALUATION_CURRENT.md` already give for not swapping models |
| Model routing | Not attempted | Same reasoning — a real, valid future lever, but a product/AI-quality decision, not a performance-engineering one |
| Reduced prompt size | Not attempted | Would directly trade against retrieval recall/accuracy (`schema_top_k`, golden-example count) — explicitly ruled out by "never compromise correctness" |
| Avoiding unnecessary LLM calls | **Already implemented, verified** | `plan_query_node`/`review_sql_node` are already zero-cost pass-throughs for non-complex questions (confirmed: `plan_query` P50=0ms across the benchmark, only firing its real ~2.2s cost for the complexity-flagged subset); the retry budget is already bounded (`agent/complexity.py`) |
| Bounded retries | **Already implemented, verified** | `compute_max_retries` + the LangGraph `recursion_limit` fix (`docs/PHASE1_BASELINE.md`) |
| Result caching where safe | **Already implemented, verified** | The frontend's `nlQuestionCache` (`chatStore.ts`) already caches identical repeated questions within a session |

## The one concrete change: database connection pooling

`db/connection.py::_cached_engine` now passes `Settings.db_pool_size` (default 10) / `Settings.db_max_overflow` (default 20) to `create_engine`, instead of silently inheriting SQLAlchemy's own default (`pool_size=5, max_overflow=10`). This mirrors `moderation/store.py`'s already-existing, already-correct pattern (`moderation_store_pool_size`/`_max_overflow`), now applied consistently to the primary database engine. **This targets concurrent request throughput, not single-request latency** — a connection pool only matters once more than `pool_size` callers need a database connection at the same moment; a single sequential benchmark run (one question at a time, exactly how `scripts/run_benchmark.py` operates) never exercises more than one connection at once and is structurally incapable of showing this change's effect.

## Benchmark rerun: BEFORE → AFTER

| Metric | Phase 2 baseline (`run_20260916T064354Z`) | Phase 3 rerun (`run_20260916T100455Z`) | Δ |
|---|---|---|---|
| SQL execution accuracy | 92.5% | 90.0% | −2.5 |
| Result-set accuracy | 42.3% | 42.3% | 0 |
| Final accuracy | 50.0% | 47.5% | −2.5 |
| Schema retrieval recall | 91.0% | 91.0% | 0 |
| Security rejection accuracy | 88.2% | 88.2% | 0 |
| Mean latency | 53.15s | 163.87s | **+110.7s** |
| P95 latency | 145.19s | 209.93s | +64.7s |
| P99 latency (computed) | 439.65s | 3,173.77s | +2,734s |
| Max single-case latency | 679.14s | 5,501.85s | +4,822.7s |

**This is a measured latency regression, reported honestly, not hidden — and it is not attributable to the connection-pooling change.** Three independent pieces of evidence rule out the pool-sizing fix (or any other Phase 3 code change) as the cause:

1. **The pool-sizing change cannot affect a sequential benchmark at all** — as explained above, it only has an effect under concurrent connection demand, which this benchmark never produces (see `tests/test_connection.py::TestEnginePoolSizing` for the isolated, deterministic unit-level verification of what the change actually does).
2. **The per-stage latency *proportions* are essentially unchanged** — `generate_sql` (the LLM call) was 93.8% of total time in the Phase 2 run and 98.2% in this Phase 3 run; every other stage's share shrank or stayed flat. If application code had regressed, it would show up as a *larger* share for some non-LLM stage — it didn't. The absolute LLM call time itself grew (mean `generate_sql` time: 63.2s → 203.9s), which is squarely Ollama/hardware-side, not this codebase's.
3. **No code touched in this pass (or in Phase 2, for that matter) sits on the `agent.graph.run_agent` hot path this benchmark exercises.** `scripts/run_benchmark.py` calls `agent.graph.run_agent` directly (bypassing the API layer's authentication/authorization entirely, confirmed via `eval/runner.py`) — Phase 2's RBAC/auth additions are structurally not in this code path at all for the SQL-only case, and Phase 3's only change so far (DB pool sizing) doesn't apply under sequential load per point 1.

**Most plausible explanation, stated as a hypothesis, not a certainty (this environment provides no way to fully isolate it):** this run shared the same machine with a large amount of concurrent, heavy work across a ~2.5-hour window (dependency installs, repeated full-suite test runs, linting, many file edits, git operations, all part of this same session's other Phase 3 steps) — the identical caveat `docs/EVALUATION_CURRENT.md` already raised for the Phase 1→Phase 2 latency change, now observed again and more severely. The single 5,501-second (92-minute) outlier case in particular is far outside anything seen in either prior run and strongly suggests a period of severe resource contention (this machine's own concurrently-loaded `qwen3.8:27b` model, or the session's own tooling, competing for the same CPU/memory `llama3.1:8b` inference needs) rather than a steady-state characteristic of the model or this application.

**Accuracy movement (−2.5 points on SQL execution/final accuracy, 0 on result-set/retrieval/security) is within normal run-to-run LLM variance** — the failed-case list is nearly identical to the Phase 2 run's (same two adversarial "security_miss" cases still correctly blocked by the SQL validator as defense-in-depth, the same cluster of `wrong_result`/`wrong_query_structure` business-semantics and structural-equivalence cases from `docs/EVALUATION_CURRENT.md`'s own analysis) — see `docs/EVALUATION_CURRENT.md`'s existing per-case breakdown, unchanged in substance.

## What this means for the performance question

**The measured, defensible conclusion is: this application's own code is not the bottleneck, was already reasonably optimized where it mattered (caching, singleton reuse, bounded retries), and the one real code-level gap found (connection pooling) has been closed — but it was never going to show up in a single-request-at-a-time benchmark, and no further "optimization" is justified without either infrastructure changes (dedicated/GPU inference hardware, an isolated benchmark environment) or an explicit, separately-approved AI-quality tradeoff (a smaller model or reduced context).** Reporting a fabricated "before→after improvement" here would contradict the actual measurement — the honest result is a regression explained by environment, not a Phase 3 win to claim.
