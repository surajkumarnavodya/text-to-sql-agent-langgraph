# Risk Register

The living list `docs/GOVERNANCE.md`'s change-control and exception
process (both already written, but previously non-functional because this
file didn't exist yet) actually points at. Two sections: **open items**
(known, standing risks — not necessarily anything to fix immediately, just
tracked so they're never silently forgotten) and **accepted exceptions**
(a control knowingly, temporarily relaxed — see `GOVERNANCE.md`'s exception
process for what belongs here and why).

Seeded from a production-readiness audit conducted 2026-09-01 (verified
against the running code and a live benchmark run, not just design review —
see `docs/PRODUCTION_READINESS_REPORT.md` for the full assessment this list
is drawn from). Owner for every item below is Suraj Kumar, the sole
maintainer (`GOVERNANCE.md`'s "Ownership").

## How to read this

- **Severity**: Critical / High / Medium / Low — potential impact if the
  risk materializes, not likelihood.
- **Status**: Open (not yet addressed) / Mitigated (a control exists but
  doesn't eliminate the risk) / Accepted (a deliberate, documented decision
  not to fix it right now, with a reason).
- **Review date**: when this entry should be looked at again, per
  `GOVERNANCE.md`'s review-cadence section — not a promise it'll be fixed
  by then, just a promise it won't be silently forgotten past that date.

---

## Open items

### R-001 — No authentication or per-user authorization

**Severity:** Critical (if deployed beyond single-user/local use) · **Status:** Open

Neither the Streamlit UI nor the API (`api/`) has a login, session
identity, or per-user authorization model. `api/auth.py`'s optional
`API_AUTH_TOKEN` is one shared secret, not per-user identity (see
`docs/API.md`'s "Auth: a lightweight hook, not a full auth system"). This
is a deliberate, documented scope boundary (`SECURITY.md`: "Not designed
for multi-tenant or production deployment"), not an oversight — but it is
the single largest blocker to a genuinely multi-user production deployment.

**Mitigation today:** document a reverse-proxy-auth deployment pattern
(`docs/DEPLOYMENT.md`) and the optional shared-token hook. Not a fix, a
documented boundary.

**Review date:** revisit if/when more than one trusted user needs access —
see `docs/PRODUCTION_READINESS_REPORT.md`'s V2 roadmap.

### R-002 — Sensitive-column classification is unpopulated

**Severity:** High (for any schema carrying real PII) · **Status:** Open

`config/sensitive_columns.yaml` ships `tables: []`. The enforcement
mechanism is real and tested (`db/value_sampling.py`,
`agent/nodes.py::validate_sql_node`, `tests/test_sensitive_columns.py`) but
protects zero columns until a human reviews and classifies the connected
database's actual columns, per `docs/GOVERNANCE.md`'s "Data classification
policy." Anyone pointing this at a database with unreviewed PII columns is
relying on the 20-distinct-value cardinality cap in `db/value_sampling.py`
alone (a side effect of a different heuristic, not a deliberate privacy
control — see that module's own docstring) for incidental protection.

**Mitigation today:** none until classification happens. This is a
per-deployment action item, not something fixable in the codebase itself.

**Review date:** before connecting to any database containing real
customer/employee PII — see `docs/PRODUCTION_CHECKLIST.md`.

### R-003 — Measured Text-to-SQL accuracy is low

**Severity:** High (for any use case where wrong answers are costly) · **Status:** Open

The most recent full live benchmark run (`eval/results/run_20260901T151533Z.json`,
57 cases, `llama3.1:8b`, against AdventureWorksDW2025) measured
**35.0% final_accuracy** and **29.6% result_set_accuracy**, despite 92.3%
`sql_execution_accuracy` (the SQL runs; it's frequently wrong). One
captured trace (`eval/results/streamlit_run.log`) shows the retry loop
regenerating byte-identical incorrect SQL across 3 consecutive retries for
one question — the self-correction mechanism doesn't reliably self-correct
in practice. `security_rejection_accuracy` is the one dimension performing
at 100%. See `docs/EVALUATION.md` for the full breakdown.

**Mitigation today:** none structural. `README.md`'s "Known limitations"
already discloses model-dependence honestly. A larger or SQL-specialized
model (`sqlcoder`, `duckdb-nsql`) is the most direct lever, untested as of
this entry.

**Update (2026-09-11, not yet re-benchmarked):** the specific failure this
entry's captured trace shows — the retry loop regenerating byte-identical
incorrect SQL — was traced to a reproducible root cause (a nested-aggregate
shape, `AVG(CASE WHEN ... THEN SUM(x) ELSE 0 END)`, that every retry kept
reaching for with no way to know why it kept failing) and addressed
structurally: a static pre-execution check now catches that shape, and two
new graph nodes (`plan_query_node`/`review_sql_node`) add an up-front plan
+ plan-conformance self-correction loop for questions matching the same
"non-trivial" signals (see `docs/ARCHITECTURE.md`). This is a plausible,
targeted fix for the *observed* failure mode, not a re-measured accuracy
number — `python scripts/run_benchmark.py --check-regression` has not been
re-run since these changes landed. Leave this entry Open until it has.

**Review date:** re-run `python scripts/run_benchmark.py --check-regression`
after any model swap or prompt change (already `GOVERNANCE.md`'s stated
cadence) — this entry should be updated with the result each time.

### R-004 — Dependency pins verified against Python 3.14, not 3.11

**Severity:** Medium · **Status:** Open

`requirements.txt`'s own header comment discloses that several DB-driver
version pins (`pyodbc==5.3.0`, `oracledb==4.0.2`, others) were selected for
`cp314` wheel availability on this project's dev machine, which only has
Python 3.14 installed — not independently verified against Python 3.11,
which is both `pyproject.toml`'s `requires-python` target and what
`.github/workflows/ci.yml` actually runs. CI passing is the closest thing
to verification this has had; no one has confirmed runtime behavior parity
beyond that.

**Mitigation today:** CI already runs the full mocked test suite against
3.11 on every push/PR — a pin that broke import/basic behavior would
likely surface there, though CI doesn't exercise every driver against a
live database of its type.

**Review date:** before relying on `DB_TYPE=mssql` or `oracle` in a new
environment — smoke-test the specific driver against Python 3.11 first
(`python scripts/test_db_connection.py`).

### R-005 — No independent security review

**Severity:** Medium · **Status:** Open (long-standing, `SECURITY.md` already discloses this)

`SECURITY.md`'s own "What is explicitly not guaranteed" section states
this has not been through an independent security review, penetration
test, or third-party adversarial prompt-injection assessment — only this
project's own reasoning, its own regression tests
(`tests/test_adversarial_input.py`, the `adversarial` eval category), and
this current audit (still one reviewer, not an independent one). Restated
here because a risk register is a more discoverable place for it than a
paragraph inside `SECURITY.md`, not because anything new was found.

**Mitigation today:** the layered design (`SECURITY.md`'s "What's actually
enforced") means a single missed prompt-injection vector is bounded by the
SQL validator's allowlist and the read-only DB role underneath it, not
unbounded.

**Review date:** before any deployment handling real user data at scale.

### R-006 — No circuit breaker / backoff on Ollama or DB connection failures

**Severity:** Low (for today's single-user, single-process usage) · **Status:** Open

`db/connection.py` has connection pooling (`pool_pre_ping=True`,
`pool_recycle=1800`) and clean failure classification, but no retry-with-
backoff on a transient connection failure, and no circuit breaker around
repeated Ollama failures (a down Ollama server fails each request
individually rather than short-circuiting after N consecutive failures).
Deliberately out of scope for this audit's hardening pass — see
`docs/PRODUCTION_READINESS_REPORT.md`'s rationale for why this wasn't
speculatively added.

**Mitigation today:** every failure mode is caught and classified, never
an unhandled crash (`ConnectionTestResult`, `OllamaUnavailableError`) — the
gap is efficiency/resilience under sustained partial outage, not
correctness or safety.

**Review date:** if/when this runs as a longer-lived multi-replica service
where a stuck dependency's blast radius grows — see V2 roadmap.

### R-007 — Agentic query planning/review LLM calls aren't rate-limited

**Severity:** Low (for today's single-user, single-process usage) · **Status:** Open

`plan_query_node` and `review_sql_node` (added 2026-09-11 — see
`docs/security-changelog.md`'s matching entry) each make their own Ollama
call but don't check `agent.rate_limit.get_llm_call_limiter`, unlike
`generate_sql_node`. Both are gated behind `agent/complexity.py`'s
"non-trivial question" signal detection and `ENABLE_QUERY_PLANNING`, so an
ordinary question is unaffected, but for a question that does trigger
them, `LLM_CALL_RATE_LIMIT_PER_MINUTE` no longer bounds the *realistic*
worst-case LLM-call count for that question the way `SECURITY.md`
previously described (see that file's "Rate limiting" section for the
corrected math).

**Mitigation today:** none — this is a genuine, currently-open gap, not a
mitigated one. The question-submission limiter
(`QUESTION_RATE_LIMIT_PER_MINUTE`) still bounds how often a new question
can even start a run, which caps the practical blast radius for today's
single-user, single-process usage.

**Review date:** before this app is exposed beyond a single trusted local
user, or if `ENABLE_QUERY_PLANNING` usage patterns suggest LLM load from
this path is material — whichever comes first.

---

## Accepted exceptions

*(None currently accepted — this section exists for `GOVERNANCE.md`'s
exception process to log into going forward. An exception logged here must
cross-reference a matching dated entry in `docs/security-changelog.md` if
it touches a change-controlled item, per that document's own rule.)*
