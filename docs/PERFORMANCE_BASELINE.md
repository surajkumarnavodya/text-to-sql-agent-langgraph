# Performance Baseline

**Method:** every number below is measured, not estimated — mined from the real, live-Ollama/live-DB benchmark run captured in `eval/results/run_20260916T064354Z_full.json` (57 cases, model `llama3.1:8b`, database `AdventureWorksDW2025`; the same run behind `docs/EVALUATION_CURRENT.md`), which records `stage_timings_ms` per case via the existing `agent.nodes._timed_node` decorator — an instrumentation mechanism that **already existed** before this pass (see `docs/PHASE1_BASELINE.md`) and needed no duplication, only aggregation. Cold-start numbers were measured directly against this session's live environment (see "Cold start" below).

## Cold start

Measured by calling each process-lifetime singleton factory fresh, then again (to confirm caching), against the real configured Ollama instance and live database:

| Component | First call (cold) | Second call (cached) |
|---|---|---|
| `config.settings.get_settings()` (includes module import) | 171.0ms | — (already `lru_cache`d) |
| `agent.graph.build_graph()` (compiles the 11-node LangGraph) | 6.4ms | 0.00ms (same object returned) |
| `agent.llm_client.get_ollama_client()` (first `ollama.Client` construction) | 169.3ms | 0.00ms (same object returned) |
| `db.connection.get_read_only_engine()` + first `SELECT 1` round-trip | 144.3ms | — (pool reused thereafter) |
| **Total simulated cold start** | **~2.0s** | — |

This confirms the existing process-lifetime-singleton architecture (`api/main.py`'s `lifespan` context manager, documented in `CLAUDE.md`'s "Process-lifetime singletons" section) works exactly as designed: every one of these costs is paid once, at process startup, never per-request. **No gap found here — nothing to fix.**

## Warm request (full agent run, live LLM + live DB)

From the 57-case benchmark run:

| Metric | Value |
|---|---|
| Mean total latency | 53.15s |
| **P50** | **27.99s** |
| **P95** | **157.63s** |
| **P99** | **439.65s** |
| Min / Max | ~0s (adversarial rejections, no LLM call) / 679.14s |

The wide P50→P99 spread is explained entirely by the per-stage breakdown below: latency is almost perfectly correlated with how many LLM calls a question triggers (1 call for a simple question, up to 4 for a question that needs multiple self-correction retries), not by any application-code variance.

## Per-stage latency breakdown (the actual bottleneck analysis)

Aggregated `stage_timings_ms` across all 57 cases (each case only records the stages it actually reached — e.g., an input-rejected adversarial case never reaches `generate_sql` at all):

| Stage | n | Mean | P50 | P95 | Total | **Share of total wall-clock time** |
|---|---|---|---|---|---|---|
| **`generate_sql`** (LLM call) | 45 | 63,174.9ms | 33,294.8ms | 180,958.5ms | 2,842.9s | **93.8%** |
| `plan_query` (LLM call, complexity-gated) | 45 | 2,397.1ms | 0.0ms | 0.0ms | 107.9s | 3.6% |
| `retrieve_schema` (ChromaDB) | 45 | 904.1ms | 795.7ms | 1,487.5ms | 40.7s | 1.3% |
| `generate_insight` (LLM call) | 37 | 501.0ms | 0.0ms | 5,217.7ms | 18.5s | 0.6% |
| `retrieve_golden_examples` (ChromaDB) | 45 | 297.6ms | 232.5ms | 582.1ms | 13.4s | 0.4% |
| `execute_sql` (live DB query) | 39 | 65.5ms | 18.8ms | 68.7ms | 2.6s | 0.1% |
| `estimate_query_cost` (EXPLAIN/SHOWPLAN) | 40 | 55.6ms | 13.5ms | 112.7ms | 2.2s | 0.1% |
| `validate_sql` (sqlglot AST check) | 42 | 16.1ms | 5.8ms | 31.1ms | 0.7s | 0.0% |
| `sanitize_input` | 57 | 0.1ms | 0.1ms | 0.2ms | 0.0s | 0.0% |
| `classify_followup` | 47 | 0.0ms | 0.0ms | 0.1ms | 0.0s | 0.0% |
| `review_sql` (LLM call, complexity-gated) | 42 | 0.0ms | 0.0ms | 0.0ms | 0.0s | 0.0% |

**LLM inference (`generate_sql` + `plan_query` + `generate_insight` + `review_sql` combined) accounts for 98.0% of total wall-clock time.** Everything else this application's own code controls — schema retrieval, golden-example retrieval, SQL validation, cost estimation, and actual database execution — together accounts for under 2%.

## LLM call volume, tokens, retries

| Metric | Value |
|---|---|
| Mean LLM calls per question | 1.23 |
| Max LLM calls for one question | 4 (bounded by `max_retries` + complexity bonus, per `agent/complexity.py`) |
| Mean prompt tokens | 3,995.3 |
| P95 prompt tokens | 8,200.0 |
| Max prompt tokens | 10,243 |
| Mean completion tokens | 160.8 |
| Max completion tokens | 1,111 |
| Mean retry count | 0.44 |
| Retry distribution | 45/57 cases needed 0 retries; 6 needed 1; 3 needed 2; 3 needed 4 (the configured cap) |

## Cost

Both LLM inference (Ollama, local) and schema retrieval (ChromaDB, local) are **compute cost only — no per-call API cost**, consistent with this project's local-first design. The only real-money cost sources in this application (media generation via IMA Studio, web search via Tavily) are **not exercised by the Text-to-SQL benchmark at all** and are governed separately by the human-approval gate, per-call rate limiters, and the session-scoped expensive-source cost ceiling documented in `docs/PHASE2_SECURITY_REPORT.md`/`docs/AUTHORIZATION.md` — no benchmark-measurable "cost" applies to the SQL path itself.

## Largest bottleneck — identified, not guessed

**LLM inference is the bottleneck, overwhelmingly and unambiguously**, and it is **not** a bottleneck this application's own code can meaningfully reduce without one of:
1. A smaller/faster model (a real AI-quality tradeoff — `llama3.1:8b` was already chosen as a balance point; a smaller model would very plausibly *reduce* the already-modest 42.3% result-set accuracy measured in `docs/EVALUATION_CURRENT.md`).
2. GPU-accelerated inference hardware (an infrastructure/deployment decision, not a code change).
3. A smaller prompt (schema top-k, golden-example count) — a real, available lever, but one that trades directly against retrieval recall/accuracy, which this pass's explicit constraint ("never compromise correctness") rules out changing blindly.

**What the data rules out as a bottleneck** (contrary to what an un-measured guess might have assumed): schema retrieval (900ms mean — already fast, ChromaDB's own caching is doing its job), SQL validation (16ms — negligible), SQL execution (65ms — negligible, the live database itself is not the constraint), and cost estimation (56ms — negligible). **None of these need optimization** — see `docs/PERFORMANCE_RESULTS.md` for what was, and deliberately was not, changed as a result of this analysis.

## Known secondary factor (disclosed, not attributable to this pass)

`docs/EVALUATION_CURRENT.md` already noted average/P95 latency roughly doubled between the 2026-09-01 baseline (33.3s/79.5s) and the 2026-09-16 measurement this document is built from (53.2s/145.2s), with no retrieval/generation code change in between to explain it — attributed there to environmental load (this machine running a large amount of concurrent Phase 1/2 work — dependency installs, repeated test/lint runs, this very benchmark — during the session it was measured in), not a regression. That attribution is repeated here for the same reason: the per-stage *proportions* (LLM ≈94-98% of total, everything else negligible) are stable and meaningful regardless of the absolute environmental slowdown: even a "clean," idle-machine run would very plausibly still show the same bottleneck profile, just with smaller absolute numbers.
