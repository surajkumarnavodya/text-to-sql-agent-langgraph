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

Neither the React dashboard (`frontend/`) nor the API (`api/`) has a login,
session identity, or per-user authorization model. `api/auth.py`'s optional
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

### R-008 — No independent authorization layer between agent routing decisions and execution

**Severity:** Critical (for a multi-tenant/multi-user deployment) · **Status:** Open

Seeded from a 2026-09-13 Agentic-AI-focused security audit covering the
multi-source orchestrator (`agent/orchestrator/`), RAG, web search, and
media generation added since the 2026-09-01 audit this register was
originally drawn from. `agent.orchestrator.nodes.classify_sources`'s LLM
output is trusted directly by `router_node`/`route_after_router` to decide
which subgraph(s) actually execute — the only gate is
`get_available_sources`, a *global*, operator-set config flag checked once,
never a per-request/per-user authorization decision. In this app's current
single-user/local-trust posture that's an accepted design boundary (same
reasoning as R-001), but it is the structural root cause of R-009 below
and would need a real policy-evaluation step (independent of the LLM's own
reasoning) before this could safely serve more than one trust tier of
caller.

**Mitigation today:** none at the authorization layer specifically — bounded
only by the same `enable_*` flags and rate limiters everything else in
this register already describes. `require_generation_approval` (see
`config/settings.py`) adds a *human*-in-the-loop gate specifically for the
one source that spends real money (media generation), which narrows this
risk's most costly instance without being a general fix.

**Review date:** revisit alongside R-001, if/when real multi-user identity
is ever built.

### R-009 — No per-user authorization on RAG document management

**Severity:** High (for a multi-tenant deployment) · **Status:** Open

`api/documents.py`'s upload/delete routes are gated only by the same
shared `API_AUTH_TOKEN` (or nothing, if unset — see R-001) as every other
route — there is no ownership check, no per-user scoping, and delete is
unconditional and irreversible. Pre-existing since this router was built;
named explicitly here following the 2026-09-13 audit rather than left
implicit inside R-001.

**Mitigation today:** none beyond the shared token. Same posture as the
Knowledge Sources page (`frontend/src/pages/KnowledgeSources.tsx`) this
API mirrors (already disclosed in `SECURITY.md`'s "Multi-source RAG and
web search" section).

**Review date:** alongside R-001 — this is a real, separate fix once
per-user identity exists (scope delete/upload to the uploading user or an
explicit content-admin role), not automatically solved by R-001's fix alone.

### R-010 — Dependency vulnerability backlog (pip-audit)

**Severity:** Medium · **Status:** Open

A `pip-audit -r requirements.txt` run on 2026-09-13 (added to CI as a
report-only step, `continue-on-error: true` — see
`.github/workflows/ci.yml`) found 65 known advisories across 10 pinned
packages, most transitive/tooling rather than this app's own code
(`chromadb`, `langgraph`/`langgraph-checkpoint`/`langgraph-sdk`,
`streamlit`, `langchain-core`, `pillow`, `python-dotenv`, `pytest`,
`black`). `streamlit` was removed from `requirements.txt` on 2026-09-13
(the app it backed was deleted — see `README.md`'s News and Updates),
which should reduce this backlog by whatever advisories were specific to
it, but the count above hasn't been re-verified with a fresh `pip-audit`
run since — treat 65/10 as the last-measured figure, not a live one. Not
triaged individually as part of that pass — several fixes are
major-version bumps (e.g. `pillow` 11→12, `langgraph` 0.2→1.0) with their
own regression risk, and `requirements.txt`'s exact pins already
have an open, related gap (R-004: verified against Python 3.14, not 3.11).

**Mitigation today:** none beyond visibility — the CI step surfaces the
current list on every run so it can't silently grow unnoticed, but nothing
blocks a merge on it yet.

**Review date:** before the next dependency-bump pass; flip
`continue-on-error` off once the backlog is triaged and pins updated.

### R-011 — Session-scoped expensive-source cost ceiling is a cost control, not an access-control boundary

**Severity:** Low · **Status:** Mitigated (partially)

`agent.rate_limit.get_session_expensive_source_limiter` (added
2026-09-13, see `docs/security-changelog.md`) caps combined
`generation`/`web` source invocations per `session_id` within a rolling
window (`SESSION_EXPENSIVE_SOURCE_LIMIT`/`_WINDOW_SECONDS`). `session_id`
is a real per-conversation correlation token, but it's client-supplied and
unauthenticated (same posture as `AskRequest.session_id` generally) — a
caller can always reset their own budget by minting a fresh one, exactly
like the existing per-IP API rate limiters (`api/rate_limit.py`) can be
evaded by changing IP. This is a genuine speed bump against accidental/
casual repeated cost, not a hard ceiling against a deliberate, motivated
caller.

**Mitigation today:** the per-source limiters (`media_gen_rate_limit`,
Tavily's own per-call cost) still apply process-wide underneath this one
regardless of session_id — a fresh session can reset its *own* budget but
not the process-wide ones.

**Review date:** alongside R-001/R-008 — a real fix requires session_id to
be tied to actual authenticated identity, not just a correlation token.

---

### R-012 — No sensitivity classification for media search library content

**Severity:** Medium (higher if the configured library could contain
sensitive imagery/audio) · **Status:** Open

Media search (`ENABLE_MEDIA_SEARCH`, off by default) indexes and makes
searchable everything under `MEDIA_LIBRARY_PATH` — including OCR'd
on-screen text, ASR transcripts, and generated captions — with **no
per-item sensitivity-tagging mechanism at all**, unlike policy RAG's
`SensitivityCategory` gate (`docs/GOVERNANCE.md`'s "Data classification
policy," extended to policy documents 2026-09-10). A photo or recording
containing something restricted (a face, a license plate, spoken PII, a
sensitive document photographed on-screen) is exactly as retrievable as
any other indexed item — there is no equivalent of the policy-RAG
"never summarized into an answer" fail-closed gate for this data shape.

**Mitigation today:** none beyond the operator's own judgment about what
goes in the configured library folder — this is the same "add the
classification/authorization layer yourself before connecting anything
sensitive" posture R-002 already states for database columns, applied to
a new data shape this project added after R-002 was written.

**Review date:** before pointing `MEDIA_LIBRARY_PATH` at any folder that
could contain sensitive imagery, recordings, or on-screen text — see
`docs/PRODUCTION_CHECKLIST.md`.

### R-013 — Content ingestion now depends on a required third-party cloud API (Azure Content Safety)

**Severity:** Medium · **Status:** Accepted (a deliberate design tradeoff, not an oversight)

The pre-ingestion moderation gate (`moderation/`, mandatory whenever
`ENABLE_MEDIA_SEARCH` or `ENABLE_DOCUMENT_RAG`/`ENABLE_POLICY_RAG` is on)
calls Azure AI Content Safety's REST API for every chunk of every file
before it can be embedded/stored. This is a real, disclosed departure from
this project's otherwise "fully local, Ollama not a hosted API" posture
(`README.md`'s "Why this project") -- unlike voice mode/media search's own
embedding path, no mainstream moderation classifier with comparable
accuracy runs fully on-device today, and this project's own design note
for the feature states that tradeoff explicitly rather than silently
picking a path. A practical consequence: ingestion for either pipeline now
requires outbound HTTPS reachability to Azure and a paid Content Safety
resource -- an Azure outage or misconfiguration blocks all new
image/video/PDF ingestion (existing, already-embedded content is
unaffected; only new ingestion is gated).

**Mitigation today:** the provider is pluggable
(`moderation.provider.SUPPORTED_MODERATION_PROVIDERS`, shaped like
`search/web_search.py`'s own provider map) -- swapping to a different
provider, or in principle a self-hosted one, is a config/code addition,
not a redesign.

**Review date:** if a locally-run moderation classifier with acceptable
accuracy becomes practical, revisit whether Azure should remain the only
implemented provider.

### R-014 — Synthetic/manipulated-media ("deepfake") detection is a disclosed placeholder, not a real control

**Severity:** Low (a soft-flag category, never a hard-reject -- see below) · **Status:** Open

`moderation/taxonomy.py`'s `synthetic_media` category exists in the
taxonomy but has **no detector wired in** -- every image/video chunk is
recorded as `"not_checked"` for this category, honestly, rather than
silently omitted or falsely presented as covered. No mainstream cloud
moderation API (including Azure Content Safety, the one provider
implemented) reliably classifies AI-generated/manipulated media as of this
writing. This category is deliberately a soft-flag, never a hard-reject,
even once a real detector is eventually plugged in -- deepfake/synthetic-
media classifiers have well-documented accuracy limits, and auto-rejecting
real user content on a false positive was judged a worse failure mode than
under-flagging.

**Mitigation today:** none -- disclosed as a known gap rather than
implemented as a weak/misleading control. `docs/RESPONSIBLE_AI.md` states
this alongside media generation's own similarly-honest content-policy
disclosure.

**Review date:** if a specific, evaluated synthetic-media detector
(cloud or local) becomes available with a stated accuracy profile worth
building a real (still soft-flag) check around.

## Accepted exceptions

*(None currently accepted — this section exists for `GOVERNANCE.md`'s
exception process to log into going forward. An exception logged here must
cross-reference a matching dated entry in `docs/security-changelog.md` if
it touches a change-controlled item, per that document's own rule.)*
