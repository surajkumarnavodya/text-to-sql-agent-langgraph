# Prompt 33 -- AI Data Analyst Agent

A governed, multi-step analyst that investigates a business question by
running bounded sub-questions through the existing Text-to-SQL pipeline, then
returns an evidence-linked report. Off by default
(`ENABLE_DATA_ANALYST_AGENT=false`).

## Pipeline

```
understand -> execute <-> analyze -> recommend -> explain -> END
```

| Node | Does | Charged to budget |
|---|---|---|
| `understand` | `check_input` on the question; one LLM call decomposes it into sub-questions (`agent/analyst/planner.py`). Any planner failure falls back to the single original question. | step + 1 LLM call |
| `execute` | Runs ONE sub-question through `agent.graph.run_agent`, the full governed pipeline. Maps the result to evidence (`agent/analyst/evidence.py`). | step + sub-question |
| `analyze` | Queues one deterministic follow-up per anomalous period found in the evidence. | step (+ follow-up when queued) |
| `recommend` | Dedupes the sub-runs' recommendations and links each to the evidence it cites. | none |
| `explain` | Writes the final status and the evidence-linked report. Always runs, even after a budget stop. | none |

Mapping of the prompt's stages to nodes: Understand = `understand`;
Plan = `understand`'s decomposition; Retrieve, Query, Validate, Execute = inside
each `execute` via `run_agent`; Analyze = `execute`'s evidence mapping plus
`analyze`; Investigate = `analyze`'s follow-ups; Explain = `explain`;
Recommend = `recommend`.

## What is reused (not duplicated)

- `agent.graph.run_agent` -- every SQL safety control: input guard, schema
  retrieval, SQL validator, restricted-column RBAC gate, row cap, timeout, the
  per-database execution limiter, cost estimation, and the self-correction loop.
  The analyst never generates, validates or executes SQL itself.
- `agent.input_guard.check_input` / `rejection_message` -- applied to the
  question and to every planner item.
- `agent.input_guard.sanitize_conversation_history` -- applied to caller history.
- `agent.model_registry.validate_model_selection` -- model allowlist.
- `analytics.engine` and `recommendation.engine` -- run inside each sub-run;
  the analyst reads their output, it recomputes nothing.
- `agent.rate_limit.get_ask_concurrency_limiter` -- the analyst shares `/ask`'s
  in-flight cap. A request past it gets the same 429.
- `api.authz.require_permission`, `api.rate_limit.enforce_api_action_rate_limit`,
  `security.oidc.real_caller_subject`, `security.tenancy.resolve_tenant_id_for_identity`,
  `security.audit_log.set_audit_tenant_id` -- the same identity and tenant
  plumbing `/ask` uses.
- `rag.llm.call_ollama` -- one new optional `model=` parameter (default `None`
  keeps every existing caller unchanged).

## API

`POST /analyst/investigate` (`api/analyst.py`, schemas in `api/analyst_schemas.py`)

Requires `Permission.ASK` **and** `Permission.EXECUTE_SQL`. Returns 404 when the
feature flag is off.

Request (`extra="forbid"`):

```json
{ "question": "Why did revenue fall in April?",
  "conversation_history": [],
  "model": null }
```

The request **cannot** carry a tenant, roles or budget. Any such field is a 422.
Tenant and roles come from the authenticated identity. The budget comes from
`Settings`.

Response:

- `status`: `succeeded` | `partial` | `insufficient_data` | `needs_clarification` | `rejected` | `failed`
- `stop_reason`: `null` | `timeout` | `step_budget_exhausted` | `subquery_budget_exhausted` | `llm_budget_exhausted` | `followup_budget_exhausted`
- `planning_mode`: `planner` | `fallback`
- `subquestions[]`: `id`, `text`, `origin` (`user` | `planner` | `investigation`), `status`, `evidence_ids`, `parent_evidence_id`
- `evidence[]`: `id`, `subquestion_id`, `claim`, `truth_level`, `kind`, `finding_kind`, `period`, `sql`, `row_count`
- `recommendations[]`: `id`, `claim`, `truth_level` (always `AI_INFERENCE`, from the engine), `category`, `confidence`, `evidence_ids`, `limitations`
- `open_items[]`: plain-language gaps (clarification needed, blocked, not run, no rows, stopped early)
- `trace[]`: one entry per step, in order (`seq`, `stage`, `status`, `detail`)
- `usage`: `steps`, `subqueries`, `llm_calls`, `followups`, `elapsed_seconds`
- `report_markdown`: the report, built from templates and typed values only

## Configuration

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `enable_data_analyst_agent` | `ENABLE_DATA_ANALYST_AGENT` | `false` | Route is 404 when off |
| `analyst_max_steps` | `ANALYST_MAX_STEPS` | 12 | Charged node executions |
| `analyst_max_subqueries` | `ANALYST_MAX_SUBQUERIES` | 4 | Sub-questions run (planner + follow-ups) |
| `analyst_max_llm_calls` | `ANALYST_MAX_LLM_CALLS` | 2 | The analyst's own LLM calls |
| `analyst_max_followups` | `ANALYST_MAX_FOLLOWUPS` | 2 | Anomaly follow-ups per analysis |
| `analyst_timeout_seconds` | `ANALYST_TIMEOUT_SECONDS` | 180 | Wall-clock deadline, checked between nodes |
| `analyst_max_recommendations` | `ANALYST_MAX_RECOMMENDATIONS` | 10 | Recommendations kept in the report |

## Controls

- **Deterministic stop rules.** `agent/analyst/budget.py` holds every limit as a
  pure function over serializable counters. The deadline is checked first, so
  no new action starts after it. An exhausted budget stops the loop, and the
  report says what did not run.
- **Bounded loops.** A period is investigated at most once per analysis,
  deduped on the period label as well as the evidence id. Each follow-up is
  charged. The graph's recursion limit is derived from `analyst_max_steps`.
- **Evidence and provenance.** Every evidence item keeps the truth level of its
  source claim. Analytics findings stay `DATABASE_FACT`. Recommendations stay
  `AI_INFERENCE`. Nothing is upgraded. A recommendation whose cited claim is not
  in the report is marked `not itemised` rather than given evidence it does not
  have.
- **Report separation.** The report has separate "Observed (database facts)"
  and "AI estimates (inference, not observed fact)" sections. No LLM writes the
  report. It is fixed templates over typed values.
- **Untrusted data.** The question and planner output pass `check_input`. A
  period label from the database is used in a follow-up only if it matches a
  short plain pattern (`_PERIOD_PATTERN`). Retrieved rows never reach an LLM
  prompt from this module.
- **Failure recovery.** A sub-run that raises is logged server-side in full and
  recorded as a failed step with a generic message. The other steps continue.
  Exception text never reaches the response.
- **Restricted data.** A restricted-column block is reported as "your role cannot
  view part of the data it needs" with the step id. No column name and no
  echoed question text appear in the response.

## Tests

`tests/test_data_analyst_agent.py` (40) and `tests/test_api_analyst.py` (8),
fully mocked at `run_agent` and `call_ollama`. The graph, budget, evidence
mapper and report renderer all run for real.

Covered: multi-step decomposition through the governed pipeline; caller roles and
tenant forwarded to every sub-run; recommendations linked to evidence;
observed/AI sections kept apart; planner failure falling back to the
single-question path; ambiguity (alone and mixed); insufficient data; a
single anomaly triggering exactly one bounded follow-up; a hostile period label
never reaching a question; the sub-query budget blocking a follow-up and saying
so; a timeout stopping before new work; injection in the question (rejected
before any SQL) and in planner output (dropped); restricted data blocked without
leaking the column; a sub-run exception recovered from; the route's feature flag;
the request rejecting smuggled tenant/roles/budget fields; the model allowlist.

Results: analyst tests 48 passed; full backend suite `tests/` 3776 passed; `tests/security/`
128 passed. `ruff`/`black` clean on new files. `mypy` clean on new files (one
pre-existing error in `db/schema_introspection.py`, not touched).

## Limitations (honest)

- **Not verified against a live Ollama or database.** Every test is mocked at
  the LLM and SQL boundary. The planner's real decomposition quality and the
  sub-question SQL accuracy are unmeasured here.
- **The timeout is not a hard cancel.** The deadline is checked between nodes. A
  sub-question already running finishes, so a single `run_agent` call can
  overrun `analyst_timeout_seconds` by its own duration. A worst-case run is
  roughly `analyst_max_subqueries` full sub-runs.
- **LLM cost is bounded per sub-run, not as a total.** The analyst counts its
  own planner call and each sub-run. It does not count the LLM calls inside
  each sub-run (generation, plan, review), which `run_agent` bounds separately.
- **No narrative.** Sub-runs run with `enable_insight=False`, so the report is
  deterministic and reads like a list. A narrative step would need its own
  grounding gate and is deliberately not added here.
- **Investigation is one template.** Only anomaly periods trigger follow-ups,
  with one fixed question ("break down by category"). It does not reason about
  causes. Root-cause attribution (Prompt 14) needs a second comparison dataset,
  which this pipeline does not yet produce.
- **Stateless and not persisted.** Analyses are not written to chat history and
  there is no server-side memory. Conversational context comes only from the
  caller-supplied history, sanitized the same way `/ask` sanitizes it.
  Tenant-safety holds because nothing is stored.
- **Shares only `/ask`'s global in-flight cap.** The per-caller concurrency cap
  is not applied here, so one caller can occupy several of the global slots at
  once. Add a per-caller limiter before enabling this for untrusted multi-tenant
  traffic.
- **Runs synchronously in the request thread pool.** No background job or
  streaming. A long analysis holds one slot for its full duration.

## Risks

- **Latency and cost** under load (see the timeout and LLM-cost limitations).
  Enable behind the flag only after measuring p95 on the real model.
- **Planner over-decomposition** can spend the sub-question budget on
  near-duplicate questions. The budget bounds the damage, but quality is
  unmeasured.
- **Report templates are English-only.** The status and section labels are not
  translated. The frontend is not wired to this endpoint yet.

## Next prompt

Recommended: **Prompt 34 -- a live evaluation harness for the analyst.** Run
the analyst against the existing `eval/benchmark` questions and a set of
multi-step business questions on the real database and model. Measure
decomposition quality, sub-question execution accuracy, budget stop rates and
p95 latency. Then decide whether to add a hard cancel (a per-sub-run timeout
through the execution layer) and a per-caller limit before enabling the flag.
