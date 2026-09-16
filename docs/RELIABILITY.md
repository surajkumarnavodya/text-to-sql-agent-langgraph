# Reliability

Phase 3 Step 5: AUDIT → COMPARE → GAP ANALYSIS → IMPLEMENT ONLY WHAT WAS MISSING.
Failure handling was reviewed dependency-by-dependency against the running code (not
assumed from docstrings); one real, evidence-backed gap was found and closed (§1); the
rest of this document records what was already correct and verified, and what was
considered and deliberately not built.

## 1. Fixed this pass: no whole-request timeout on `POST /ask`

**The gap.** Nothing bounded how long a single `/ask` request could occupy a FastAPI
worker thread. This was already flagged as a Phase 2 P1 item
(`docs/PHASE2_FINAL_REPORT.md`), and this pass's own Phase 3 benchmark run turned it
from a theoretical risk into a measured one: one real case took **~5,502 seconds
(~92 minutes)** with nothing to cut it off (`docs/PERFORMANCE_RESULTS.md`). A handful of
such requests — even if caused by nothing more malicious than a slow/overloaded Ollama
backend, not an attacker — could exhaust the server's worker-thread capacity and deny
service to every other user.

**The fix.** `api/main.py::_run_orchestrated_with_timeout` runs `run_orchestrated` on a
background thread and gives up waiting once `Settings.request_timeout_seconds` (default
600s / 10 minutes) elapses, returning a clean `status="failed"` response instead of
blocking. This reuses the exact pattern `db/execution.py::_execute_with_timeout` already
established for query execution (background thread + `join(timeout)`) rather than
inventing a second mechanism. Honestly caveated in the setting's own docstring: this is
"stop waiting," not true cancellation — there is no clean cross-thread way to abort an
in-flight Ollama call, so the abandoned thread keeps running to completion with its
result discarded. What it buys is a *timely response to the caller* and a *freed
request-handling thread*, not reduced load on the LLM backend from the already-in-flight
call. Deliberately scoped to the API layer only (`api/main.py`), not inside
`agent.graph.run_agent`/`run_orchestrated` themselves, so `eval/runner.py`'s benchmark
keeps measuring real, untruncated completion times — baking a timeout into the graph
itself would have corrupted the exact P95/P99/max measurements
`docs/PERFORMANCE_BASELINE.md` and `docs/PERFORMANCE_RESULTS.md` depend on.

Verified by `tests/test_api_ask.py::TestAsk::test_request_timeout_returns_a_failed_status_not_a_hang`
(a real `time.sleep()` past a 1-second configured timeout, exercising the actual
background-thread-join path, not just a mocked exception) — full suite green
afterward (1,017 passed).

## 2. Already correct, verified (not duplicated)

| Dependency | Failure mode | Existing handling | Verified |
|---|---|---|---|
| Ollama (SQL generation) | Unreachable/timeout | `OllamaUnavailableError` raised, caught in `generate_sql_node`, terminates the request as `status="failed"` immediately — **deliberately not retried** (see §3) | `agent/nodes.py:773-783` |
| Ollama (query planning) | Unreachable/timeout | Fails open: proceeds with no plan, logs a warning (`plan_query_node` is an accuracy aid, never a blocker) | `agent/nodes.py:659-662` |
| Ollama (plan review) | Unreachable/timeout | Fails open: treats the unreviewed SQL as passing | `agent/nodes.py:828-834` |
| Ollama (insight generation) | Unreachable/timeout | Fails open: omits the insight, the query result itself is still returned | `agent/nodes.py:1429-1434` |
| Ollama (media captioning) | Unreachable/timeout, blank model config | Fails open: segment is indexed and searchable via ASR/OCR text alone (`media/captioning.py`, per `CLAUDE.md`) | Verified via `CLAUDE.md`'s documented contract; unchanged this pass |
| Database (query execution) | Timeout, connection drop | Hard wall-clock cutoff via background-thread + forced connection-close (`db/execution.py::_execute_with_timeout`), driver-level `SET statement_timeout` where supported, row-cap via `fetchmany` independent of `LIMIT` | `db/execution.py:61-121` |
| Database (any configured DB down) | Unreachable | `GET /health` reports that one database `degraded` without taking down the others or the process (`db.connection.test_connection`, one call per configured database) | `api/main.py:436-479` |
| Database (misconfigured *additional* database at startup) | Bad config for one of several `DB_CONNECTIONS` entries | Startup continues; only that database's engine fails to pre-warm, logged, `/health` still reports it | `api/main.py:124-137` |
| Web search (Tavily) | Not configured, request failure, outage | Fails open per-source: returns a `status="failed"` `SourceAnswer` for that one source, does not crash the orchestrator run or other fired sources | `agent/orchestrator/nodes.py:502-516` |
| Document/policy RAG | Retrieval/grading/generation failure | Bounded retry (rewrite → retry, `RAG_MAX_RETRIES`), then an explicit insufficient-information fallback rather than a crash or a hallucinated answer | `rag/graph.py`, per `CLAUDE.md` |
| Media generation (IMA Studio) | Provider failure, download failure, content-policy rejection | Each stage caught individually and logged as a distinct `security.audit` event (`generation_provider_failed`/`generation_download_failed`/`generation_rejected`), returns a clean failed result, never propagates | `agent/orchestrator/nodes.py` (§ line ~716-780) |
| Golden-example retrieval | Chroma error, empty/missing collection | Fails open: proceeds with no examples | Per `CLAUDE.md`; unchanged this pass |
| Moderation gate | Missing provider/store config | **Fails closed** (`ModerationNotConfiguredError`) — the one dependency in this list that intentionally does NOT degrade gracefully, since silently skipping content moderation would be a security regression, not a reliability win | Per `CLAUDE.md`'s "Content moderation gate" section |
| OIDC JWKS endpoint | Unreachable at token-validation time | Token validation fails closed (rejects the request) — the only correct behavior for an auth boundary; not a "degrade gracefully" candidate | `security/oidc.py` |

## 3. Considered, not implemented — and why

**Circuit breakers.** Not added, for two concrete reasons rather than time pressure:
(1) no measured evidence of repeated-failure storms exists in this application's actual
operating history — every timeout/benchmark run this project has captured shows *slow*
external calls, not *failing* ones (Ollama and the configured databases were reachable
throughout every benchmark run in `eval/results/`); (2) this app's own stated scale
(`README.md`'s Limitations section: "single-user, local-dev oriented") means the
scenario a circuit breaker protects against — many concurrent callers all blocked
waiting on the same downed dependency, worth short-circuiting after N failures to shed
load — doesn't really apply the way it would for a multi-tenant service. §1's
whole-request timeout already solves the concrete, measured problem (one request tying
up a worker thread indefinitely); a circuit breaker on top would be defending against a
failure mode not yet observed, which the Phase 3 brief's "implement only where
justified by measurement" instruction explicitly cautions against.

**Automatic retry-with-backoff for external calls (Ollama, Tavily, moderation API,
media-gen providers).** Deliberately **not** added, and the *existing* choice to fail
fast on an LLM connectivity error (§2's first row) is itself a considered design, not an
oversight: `agent/nodes.py`'s own comment (`generate_sql_node`, near line 1476) states
plainly that burning the bounded self-correction retry budget on "a problem retrying
can't fix" (an infrastructure outage, not a SQL-quality issue) would be wrong — that
budget exists for iterating on the LLM's *output*, not for papering over a downed
dependency. Adding a *second*, separate retry-with-backoff layer specifically for
connectivity blips was considered and rejected for this pass: it would extend a
request's latency exactly when `Settings.request_timeout_seconds` (§1) is trying to
bound it, no measured "flaky-then-recovers" pattern exists in this project's actual
runs to justify it, and it would add real complexity (a new dependency or hand-rolled
backoff loop, at every one of five distinct external-call sites) for a failure mode
that, when it has occurred in practice, has been "down for a while," not "flaky for one
call." A future session with real evidence of transient (not sustained) failures would
have a much stronger case for adding this than this pass does.

**Graceful degradation** was **not** newly added anywhere in this pass — as §2 shows,
it was already implemented, thoroughly, at essentially every optional/enrichment layer
(planning, review, insight, golden examples, media captioning, per-source multi-source
answers). Nothing found here needed a new fail-open path; the audit confirmed existing
coverage rather than finding a gap to fill.

## 4. Named gaps, disclosed rather than hidden

- **A single in-flight request has no automatic resume/retry if it fails** — a user
  whose question hits an Ollama outage mid-request gets a clean failure message (never
  a hang, never a raw exception, per §1-2) but must manually resubmit; there is no
  session-level "retry this question when the dependency recovers" mechanism. Consistent
  with this app's stateless-API design (`AskRequest.session_id` is a correlation token,
  not a resumable job — see `CLAUDE.md`'s "Process-lifetime singletons" section) and
  judged acceptable for the target scale; a real limitation for a future multi-tenant
  deployment.
- **`§1`'s timeout frees the request-handling thread but not backend load** — stated
  plainly in that section already; repeated here because it's the most important caveat
  in this whole document. If Ollama is genuinely wedged, every abandoned background
  thread from a timed-out request keeps consuming CPU/memory until Ollama itself
  recovers or the process restarts. This fix improves the *caller's* experience, not the
  server's total resource ceiling under sustained backend failure.
- **`media_gen`/`search`/`moderation` external-API failures are not distinguished from
  rate-limit responses** (e.g. Tavily/IMA returning HTTP 429) — both are caught by the
  same broad exception handling and treated as "this source/action failed," not as "back
  off and retry shortly." Given §3's reasoning against adding backoff generally, this is
  an accepted consequence of that same decision, not a separate oversight.
