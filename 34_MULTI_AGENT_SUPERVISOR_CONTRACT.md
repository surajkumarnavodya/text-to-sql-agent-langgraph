# Prompt 34 -- Multi-Agent Supervisor

A governed supervisor that routes one analysis turn across typed specialist
agents, behind `ENABLE_MULTI_AGENT_SUPERVISOR` (default off). Served at
`POST /analyst/supervise`. The Prompt 33 analyst (`POST /analyst/investigate`)
and `/ask` are unchanged.

## Is multi-agent justified here? (The decision)

Inspected first: the existing LangGraph is a single deterministic state
machine (`agent/graph.py`, 18 nodes) plus the Prompt 33 analyst graph. The
only LLM-driven routing already in the codebase is the multi-source router,
which fans out to sources. The specialists this prompt names are mostly
**pure functions** over data that already exists: analytics, forecasting,
root cause, recommendation linking, governance. Only the planner calls a model.

Decision: **no LLM-to-LLM agent mesh.** A model choosing which agent to call
next, or agents messaging each other, adds unbounded routing, cost and
injection surface without adding capability. Instead, a **plain-Python
supervisor** applies a fixed routing policy. Every routing decision is
inspectable, and every refusal (budget, circuit, timeout, permission) is an
explicit, testable value. Agents return typed outputs and never talk to each
other.

LangGraph was not used for the supervisor, because the routing has no branches
that need graph state. The Prompt 33 graph is kept as-is and is the comparison
baseline.

## Specialist agents

Each agent is an `AgentSpec` (`agent/multiagent/contracts.py`): its allowed
tools, its truth-level ceiling, its cost per call, and its retry count.

| Agent | Kind | Allowed tools | Truth ceiling | Cost | Does |
|---|---|---|---|---|---|
| planner | llm | `llm_plan` (ASK) | AI_INFERENCE | 3 | Splits the question. Output is text only. |
| governance | control | none | AI_INFERENCE | 0 | Refuses sensitive requests. Redacts rows before any engine sees them. |
| semantic | retrieval | `business_context` (ASK, 2 attempts) | CONFIRMED_BUSINESS_TRUTH | 2 | Governed metric definitions. |
| sql_data | data | `governed_sql` (EXECUTE_SQL, 1 attempt) | DATABASE_FACT | 5 | The only path to data. Wraps `run_agent`. |
| analytics | engine | `analytics_engine` (ASK) | DATABASE_FACT | 1 | Statistics over the governed rows. |
| forecast | engine | `forecast_engine` (ASK) | AI_INFERENCE | 1 | Projections. Runs only for a forecast intent. |
| root_cause | engine | `root_cause_engine` (ASK) | AI_INFERENCE | 1 | Attribution. Runs only for an explicit comparison pair. |
| recommendation | engine | none | AI_INFERENCE | 0 | Reuses the analyst's recommend node, unchanged. |

## Reused components

- `agent.tools.registry.ToolRegistry` and `agent.tools.types` (`Tool`,
  `RetryPolicy`, `ToolPermissionError`): permission check, timeout, retry and
  audit. The supervisor adds the per-agent allowlist on top.
- `agent.graph.run_agent`: the governed SQL pipeline, unchanged.
- `agent.analyst.planner.plan_subquestions`, `agent.analyst.evidence.build_outcome`,
  `agent.analyst.graph.recommend_node`: Prompt 33's planner, evidence mapper and
  recommendation linker, all reused directly.
- `analytics.engine.compute_analytics_result`, `analytics.forecasting.generate_forecast`,
  `analytics.root_cause.investigate_root_cause`: called as-is.
- `governance.request_policy.evaluate_request`, `governance.result_policy.govern_rows_for_caller`:
  the request gate and row redaction.
- `retrieval.retriever.retrieve_business_context`, `extract_governing_metrics`.
- `agent.input_guard.check_input`, `rejection_message`, `sanitize_conversation_history`.
- `agent.provenance.DataTruthLevel`: the only truth vocabulary.
- `security.audit_log`, `api.authz.require_permission`, `api.rate_limit`,
  `agent.rate_limit.get_ask_concurrency_limiter`: the same identity, audit and
  admission controls `/ask` uses.

## Supervisor policy

`agent/multiagent/supervisor.py`. Per turn, in order:

1. Input guard on the question (no cost). A rejection ends the turn.
2. Governance `request`: a `refuse` or `require_authorization` decision ends
   the turn before any data is touched.
3. Planner, then each sub-question re-checked by the input guard. A planner
   failure falls back to the single question.
4. Semantic definitions, once, for the tenant's first database.
5. For each data task: `sql_data` -> governance `rows` -> `analytics` -> `forecast`
   (conditional). Only governed rows reach an engine.
6. `root_cause` for each explicit `RootCausePair`. Never planner-created.
7. `recommendation`, over the data tasks' recommendations.
8. Validation (`agent/multiagent/resolution.py`), conflict resolution, status,
   report.

Every call passes these checks, in order, before it runs: the wall-clock
deadline, the turn budget (call count and cost units), and the agent's
circuit breaker. A refusal by any of them is recorded in the trace and in the
open items.

## Controls

- **Tool allowlist and identity lock** (`ToolGateway`, `policy.py`). The
  gateway is bound to the invoking agent's own spec. A handler cannot name
  another agent's spec. A tool outside the allowlist is denied. Input that
  sets `caller_roles`, `tenant_id` or `actor` is denied. The real caller's
  roles are used for the registry's own permission check.
- **Truth-level ceiling.** A claim above its agent's ceiling is lowered to the
  ceiling, and the lowering is reported. An agent cannot make an inference
  look like a fact.
- **Evidence grounding.** A claim must cite evidence the supervisor issued
  (`Workspace.evidence_ids`). Invented evidence is dropped.
- **Attribution.** A claim must name its own agent and a task this turn
  dispatched. Anything else is dropped.
- **Conflicts** (`resolve_conflicts`), a fixed rule, never by agent order:
  - two observed values that disagree: both withheld, and the report says so;
  - an observed value and an inference that disagree: the observed value wins,
    and the inference is superseded;
  - two inferences that disagree with no observed value to decide: both withheld.
  Values are averaged never, and rounding differences are not conflicts.
- **Circuit breakers** (`CircuitBreaker`), keyed by **tenant and agent**. One
  tenant's failures cannot switch an agent off for another tenant. Opens after
  `multiagent_circuit_failure_threshold` consecutive failures, probes once
  after `multiagent_circuit_cooldown_seconds`.
- **Retries.** Only where a retry can help: `business_context` retries once.
  `governed_sql` does not retry, because `run_agent` already runs its own
  bounded self-correction loop and a whole-call retry would multiply its cost.
- **Failure isolation.** A specialist that raises is recorded as `failed`
  with the fixed text `internal error`. The exception goes to the server log
  only. The planner and semantic lookup are fail-open enrichment, so their
  failure adds an open item and does not make the turn `partial`.
- **Raw rows** live in an in-memory workspace for one turn. They never enter
  the trace, the report or the response.

## Status

- `succeeded`: every step finished cleanly, with data.
- `partial`: a budget or timeout stopped part of the turn, a data step failed
  or was denied, a conflict was withheld, or an agent output was rejected.
- `refused` / `rejected`: governance or the input guard stopped the turn before data.
- `needs_clarification`, `insufficient_data`, `failed`: as in the analyst.

## API

`POST /analyst/supervise` (`api/analyst.py`, schemas in `api/analyst_schemas.py`).
Same request body and identity rules as `/analyst/investigate`: requires
`ASK` and `EXECUTE_SQL`, rejects client-supplied tenant, roles or budget (422),
validates the model against the allowlist (400), and shares the `/ask`
concurrency cap. Returns 404 while the flag is off.

Response: `status`, `stop_reason`, `subquestions`, `claims` (each with `agent`,
`task_id`, `text`, `truth_level`, `grounded_in`), `conflicts` (task, key,
agents, outcome; never values), `violation_count` (a count, never the rejected
text), `open_items`, `trace`, `usage`, `report_markdown`.

## Configuration

| Setting | Env var | Default |
|---|---|---|
| `enable_multi_agent_supervisor` | `ENABLE_MULTI_AGENT_SUPERVISOR` | `false` |
| `multiagent_max_agent_calls` | `MULTIAGENT_MAX_AGENT_CALLS` | 16 |
| `multiagent_max_cost_units` | `MULTIAGENT_MAX_COST_UNITS` | 40 |
| `multiagent_circuit_failure_threshold` | `MULTIAGENT_CIRCUIT_FAILURE_THRESHOLD` | 3 |
| `multiagent_circuit_cooldown_seconds` | `MULTIAGENT_CIRCUIT_COOLDOWN_SECONDS` | 60 |

The per-turn timeout reuses `analyst_timeout_seconds`. The `.env.example`
block lists them.

## Benchmark: supervisor versus the Prompt 33 analyst

`eval/multiagent_benchmark.py`, run with `python -m eval.multiagent_benchmark`.
Both flows run the same scenarios, with the same fake LLM and the same fake
governed SQL. The table comes from that run.

```
scenario              flow                      status               sql  llm  spec  obs        ms
--------------------------------------------------------------------------------------------------
single_lookup         analyst (LangGraph)       succeeded              1    1     0    1      9.68
single_lookup         supervisor (multi-agent)  succeeded              1    1     5    1      6.07
two_step_question     analyst (LangGraph)       succeeded              2    1     0    2      4.65
two_step_question     supervisor (multi-agent)  succeeded              2    1     7    2      6.13
forecast_intent       analyst (LangGraph)       succeeded              1    1     0    1      3.30
forecast_intent       supervisor (multi-agent)  succeeded              1    1     6    1      3.48
planner_unavailable   analyst (LangGraph)       succeeded              1    1     0    1      2.89
planner_unavailable   supervisor (multi-agent)  succeeded              1    1     5    1      2.99
one_step_fails        analyst (LangGraph)       partial                2    1     0    1      3.75
one_step_fails        supervisor (multi-agent)  partial                2    1     5    1      2.69
```

Columns: `sql` = governed SQL executions, `llm` = planner LLM calls, `spec` =
non-skipped specialist invocations, `obs` = observed row-count claims, `ms` =
wall time (reported, not asserted, and noisy at this size).

Reading it:
- **Same data, same cost.** Both flows make identical SQL and planner calls and
  report the same observed rows. The supervisor adds no SQL and no LLM call.
- **The overhead is deterministic specialist work**, 5 to 7 invocations per
  turn, all in-process. On these fakes its wall-time cost is in the noise.
  It is not measured against a real model or database here.
- **The supervisor does more.** Its `forecast_intent` row includes a projection
  the analyst does not produce. The analyst's `analytical_intent` is ignored
  there.
- **The analyst does more in one place.** It runs anomaly follow-up
  investigations (see Limitations).

Regression tests: `tests/test_multiagent_benchmark.py` asserts the equalities
above (SQL and planner calls, observed data, partial status on a failing
step, the analyst's zero specialist overhead) and does not assert timing.

## Tests

- `tests/test_multiagent_supervisor.py` (43): routing and happy path with the
  real analytics and forecasting engines; the request gate (refusal, injection);
  planner fallback and caller scope (viewer denied SQL; user's roles reach every
  sub-run); malicious tool requests (allowlist, identity forgery, role widening
  through input); output validation (invented evidence, truth-level escalation,
  undispatched task, impersonation); conflict resolution (two facts, fact vs
  inference, rounding, withheld inferences); failure recovery (isolated query
  failure with no error text leaked, raising specialist, retry, persistent
  failure, circuit breaker opening and probing); budgets and timeouts (call
  budget, cost budget, deadline); root cause only for explicit pairs; analytics
  receives only governed rows; per-tenant breakers.
- `tests/test_multiagent_benchmark.py` (12): the comparison above.
- `tests/test_api_multiagent.py` (5): flag, response shape, no echo of rejected
  content, server-side identity.

Results: 3,916 backend tests pass (the full suite, including `tests/security/`).
`ruff` clean on new files, `mypy` clean on new files. The one remaining mypy
error is pre-existing in `db/schema_introspection.py`.

## Limitations (honest)

- **Not verified against a live model or database.** The benchmark is
  orchestration accounting with fake LLM and SQL. It does not measure answer
  quality.
- **The supervisor does not do the anomaly investigation loop.** Prompt 33's
  analyst re-queries an anomalous period. The supervisor flags an anomaly in
  analytics output but does not follow up. Merging the two loops is the
  obvious next step.
- **Root cause is not reachable from the API.** It runs only when a caller
  passes `RootCausePair`s programmatically. The planner cannot create one. This
  matches the Prompt 14 precedent: built and tested, not wired to a live
  trigger.
- **Timeouts are checked between dispatches.** A specialist already running
  finishes, and a tool-level timeout (`ToolRegistry`) leaves the worker thread
  running in the background. The same disclosed behavior as Prompt 33.
- **Breakers and budgets are process-local.** A multi-worker deployment has one
  independent breaker set per worker, the same limit as the other rate limiters.
- **Semantic definitions read only the tenant's first database.**
- **The report carries database-derived labels as text.** `report_markdown`
  includes values from analytics and forecast claims. Any UI that renders it as
  Markdown must escape it. The frontend is not wired to this endpoint yet.
- **Raw rows are held in memory for one turn.** Row volume is capped by
  `max_result_rows` in `run_agent`. There is no separate cap on the supervisor's
  workspace.

## Risks

- **A latent privilege-escalation path in the shared registry.**
  `ToolRegistry.execute` lets input data override `caller_roles`
  (`agent/tools/registry.py`, `call_input.setdefault`). An existing test pins
  this behavior (`tests/test_tools_registry.py::test_explicit_caller_roles_in_input_data_wins`).
  The permission check uses the real roles, but the handler receives the
  override. Nothing on the HTTP path passes user input to the registry today, so
  the exposure is latent. This prompt's `ToolGateway` blocks it for everything it
  runs. **Recommended fix:** make the registry always overwrite `caller_roles`
  and update that pinned test. I did not change shared behavior unilaterally.
- **Cost under real models.** Each turn can make a planner call and several
  governed SQL runs, each with its own LLM calls. Measure p95 on the real
  model before enabling.
- **Tenant-scoped breakers still share one process.** A noisy tenant can still
  consume the global in-flight cap, as with `/ask`.

## Next prompt

Recommended: **Prompt 35 -- merge the investigation loop into the supervisor and
close the registry escalation.** Give the supervisor the anomaly follow-up that
Prompt 33 has, with the same bounds. Make `ToolRegistry` always overwrite
`caller_roles`, and update the pinned test. Then run the live evaluation (the
Prompt 34 benchmark, plus the real-model questions from Prompt 33's next
section) to decide whether the supervised route should replace the analyst
route.
