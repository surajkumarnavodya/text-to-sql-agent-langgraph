# PROMPT 23 — Observability, Evaluation & Reliability

Committed verbatim, per `00_MASTER_IMPLEMENTATION_CONTRACT.md`'s own
instruction that every prompt in the 32-prompt series is executed against
that standing contract, not just the one that introduced it.

## CLAUDE CODE PROMPT

### Role
Act as a Principal Architect, senior engineer and enterprise product engineer responsible for safely extending an existing production-oriented AI platform.

### Objective
Create complete operational visibility and evaluation.

### Mandatory first step
Read `00_MASTER_IMPLEMENTATION_CONTRACT.md`.

Then inspect the repository before changing code.

### Inspect before implementation
Inspect existing logging, tracing, metrics and evaluation framework.

### Implementation requirements
Trace request, authentication, tenant, intent, retrieval, plan, SQL generation, validation, execution, analytics, visualization, recommendation and feedback. Capture correlation IDs, duration, status, model/cost metadata where available, DB provider, query fingerprint and result size without sensitive values. Build evaluation datasets for SQL, semantic, planning, analytics, recommendations, security and tenant isolation. Add regression gates.

### Testing requirements
Test trace continuity and evaluation regression behavior.

### Acceptance criteria
Failures can be attributed to data, retrieval, model, planning, SQL, database, analytics or recommendation stages.

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

The inspection found far more existing machinery than expected: every
node in the LangGraph pipeline was already individually timed
(`agent.nodes._timed_node`), correlation IDs were already stamped onto
every log line (`security.audit_log.CorrelationIdLogFilter`), and a
mature SQL benchmark plus a 500-case security benchmark already existed.
The two real, concrete gaps found and closed:

1. **Tenant was missing from ordinary logs.** Only `security.audit`
   events carried it; a new `TenantIdLogFilter`, mirroring the existing
   correlation-ID filter exactly, closes this for every log line with
   zero per-call-site changes.
2. **No per-stage field beyond duration existed to attribute a failure
   to a specific stage.** `_timed_node` now also emits an additive
   `[trace]` line carrying status/error-category/model/db-provider/
   query-fingerprint/result-size, each read from already-existing state
   keys, each omitted (never a literal `None`) when not applicable.

For evaluation: three of the seven named domains (SQL, security, tenant
isolation) already had real, superior coverage and were deliberately
left untouched (reused, not duplicated). The remaining four (semantic,
planning, analytics, recommendations) had none — closed by a new,
fully-deterministic `eval/component_benchmark/` package, with a
case-level regression gate and a committed baseline, wired into the
ordinary `pytest` suite since every one of the four domains it covers is
a pure function with no live-infrastructure dependency.

See `CLAUDE.md`'s "Observability, evaluation & reliability" section and
`docs/OBSERVABILITY.md`'s §3/§3a for the full design, and the session's
final report for the complete INSPECT → REPORT writeup.
