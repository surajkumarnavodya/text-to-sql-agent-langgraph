# PROMPT 24 — Full Integration & Regression

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Validate all backend capabilities together.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Review Prompt 01 baseline and all changes from prompts 02–23.

### Implementation requirements
Run unit, integration, API, adapter, SQL security, semantic, analytics, recommendation, tenant isolation, frontend and load tests as applicable. Trace journeys: basic Text-to-SQL; governed analytics; insight; recommendation; new-client onboarding; unauthorized access. Compare critical baseline behavior. Fix regressions and update documentation.

### Testing requirements
Execute the complete relevant test suite and representative end-to-end flows.

### Acceptance criteria
No critical regression remains unresolved.

### Non-functional requirements
- Preserve existing functionality.
- Avoid duplicate implementations.
- Enforce authentication, authorization and tenant isolation.
- Never allow the LLM to bypass deterministic security controls.
- Keep secrets out of source code and logs.
- Add structured logging/tracing using the existing observability framework.
- Keep APIs backward compatible unless a documented migration is required.
- Update documentation and configuration examples.

### Required execution lifecycle
INSPECT → PLAN → IMPLEMENT → TEST → REVIEW → FIX → DOCUMENT → REPORT

Before broad implementation, present the implementation plan. After implementation, run relevant tests and fix regressions.

### Required final report
Report:
1. Areas inspected.
2. Existing functionality reused.
3. Files created/modified.
4. API/config/database changes.
5. Tests added.
6. Tests executed and results.
7. Security findings.
8. Tenant-isolation findings.
9. Performance implications.
10. Known limitations.
11. Remaining risks.
12. Recommended next prompt.

## Outcome summary

The central finding: **every node's own unit-level contract was already
well tested, but nothing in this codebase had ever proven the 18 real
nodes compose correctly through the actual compiled LangGraph** — every
existing test either called one node function directly in isolation, or
mocked `agent.graph.run_agent` itself at the API boundary. New
`tests/test_full_pipeline_integration.py` closes that gap: five tests
that call the real, compiled graph end-to-end (mocking only the genuine
network/DB boundary — the schema/golden-example/business-context
retrieval functions, the seven Ollama-calling functions, and
`execute_readonly_sql`) covering the basic Text-to-SQL journey, the
self-correction retry loop actually looping, a declining-metric result
flowing through the real analytics/recommendation/insight pipeline
together, governed-metric conformance review actually engaging mid-run,
and an injection attempt being rejected before any generation/execution
mock could even be reached. All five passed without needing any
production-code fix — a real, measured confirmation that the 18-node
graph still composes correctly, not an assumption.

Two further gaps found and closed while validating against the Prompt 01
baseline: `tests/test_sql_agent_integration.py
::test_existing_nodes_are_all_still_present` had silently drifted out of
sync with the real graph (missing four of eighteen real nodes from its
own checklist, now exhaustive); and `docs/ARCHITECTURE.md`'s
`StateGraph` diagram and per-node writeup were stale since roughly
Prompt 8/9, still describing "the twelve nodes" and missing all six
nodes added since (`classify_analytical_intent`, `build_analytical_plan`,
`review_metric_conformance`, `compute_analytics`, `generate_forecast`,
`generate_recommendations`) — now updated to the real, current
eighteen-node graph, diagram included.

The "new-client onboarding" and "unauthorized access" journeys were
confirmed to already have adequate, real end-to-end coverage
(`tests/test_api_onboarding.py::TestFullLifecycle`,
`tests/test_api_authz.py`'s vertical/horizontal privilege-escalation
suite) and were deliberately not duplicated. The frontend test suite
(292 tests) and production build were both run fresh and confirmed
green/clean, with zero backend-driven regressions found.

See `CLAUDE.md`'s "Full integration & regression" section for the full
write-up, and the session's final report for the complete
INSPECT → REPORT accounting.
