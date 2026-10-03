"""The component benchmark -- Prompt 23 (observability, evaluation &
reliability).

Deliberately separate from `eval/`'s own SQL benchmark (`eval.schema`/
`eval.runner`/`eval.regression`, which needs a live database and a live
Ollama model) and from `eval/security_benchmark/` (needs both of those
*and* the multi-source router) -- this one exercises four domains that
are, by construction, pure/deterministic functions with no I/O:

- **semantic** -- governed-metric rendering determinism
  (`agent.llm_client._build_mandatory_metrics_block`).
- **planning** -- the analytical-plan validator
  (`agent.plan_validator.validate_plan`).
- **analytics** -- the deterministic analytics engine
  (`analytics.engine.compute_analytics_result`).
- **recommendations** -- the evidence-first recommendation engine
  (`recommendation.engine.generate_recommendations`).

Because every case calls a real, already-shipped production function
directly (never a mock, never a re-implementation of that function's own
logic), this harness is fully runnable in CI on every PR
(`tests/test_eval_component_benchmark.py`) -- unlike the SQL/security
benchmarks, which stay manual (`scripts/run_benchmark.py`/
`scripts/run_security_benchmark.py`) because they need a live DB/LLM this
environment doesn't have.

**Not duplicated, reused where it already exists:** "SQL" (the existing
benchmark), "security" (`eval/security_benchmark/`), and "tenant
isolation" (`tests/security/test_cross_tenant_isolation.py`/
`test_cross_tenant_shared_infrastructure.py`, already a real, CI-enforced
regression suite) are intentionally NOT re-covered by a parallel dataset
here -- see `CLAUDE.md`'s "Observability, evaluation & reliability"
section for the full reasoning.
"""
