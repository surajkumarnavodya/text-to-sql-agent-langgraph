# Security Changelog

A dated log of every deliberate change to a security-relevant control,
separate from ordinary feature/commit history, per
[`GOVERNANCE.md`](GOVERNANCE.md)'s change-control section. This is where
"when and why did X threshold change" gets answered without reconstructing
it from `git log` across unrelated commits.

**In scope:** any change to the SQL validator's allowlist
(`agent/sql_validator.py`), the column-sensitivity classification config
(once it exists — see `GOVERNANCE.md`'s data classification policy),
rate limits (`QUESTION_RATE_LIMIT_PER_MINUTE`,
`LLM_CALL_RATE_LIMIT_PER_MINUTE`, `API_ACTION_RATE_LIMIT_PER_MINUTE`,
`MEDIA_GEN_RATE_LIMIT`/`_WINDOW_SECONDS`,
`SESSION_EXPENSIVE_SOURCE_LIMIT`/`_WINDOW_SECONDS`), cost-estimation
thresholds (`COST_MODERATE_ROW_THRESHOLD`, `COST_HIGH_ROW_THRESHOLD`,
`COST_ESTIMATION_ENABLED`), the media-generation content-policy check
(`agent.orchestrator.nodes._DISALLOWED_PROMPT_SUBSTRINGS`), or the
media-generation human-approval gate (`REQUIRE_GENERATION_APPROVAL`).

**Entry format:** date, what changed (old → new), why, and whether it's a
permanent change or a time-boxed exception (cross-reference the matching
entry in [`RISK_REGISTER.md`](RISK_REGISTER.md)'s "Accepted exceptions"
section if the latter).

---

## 2026-09-01 — Governance process established

**Change:** No control values changed. This changelog, `GOVERNANCE.md`,
`COMPLIANCE.md`, `RESPONSIBLE_AI.md`, and `RISK_REGISTER.md` were added,
formalizing change control for the items listed above going forward.

**Why:** Prior to this date, changes to the validator allowlist, rate
limits, and cost thresholds were made via ordinary commits with no
dedicated audit trail separate from general project history — reasoned
about carefully at the time (see `SECURITY.md`'s calibration notes on the
cost thresholds, for instance) but not logged as a distinct, reviewable
category of change.

**Status:** Permanent (process change, not an exception).

---

## 2026-09-01 — Enterprise security audit: validator hardening + defense-in-depth additions

**Change:** A comprehensive security audit (22 attack-surface categories)
was performed against the running code, not just the design -- concrete
attack payloads were run through `agent.sql_validator.validate_sql` in this
repo's own environment rather than assumed safe from the design alone. That
surfaced two confirmed gaps, both closed in `agent/sql_validator.py`'s
allowlist (the item this changelog exists specifically to track):

1. **Critical -- closed.** The validator only checked the parsed
   statement's *root* AST node type. A data-modifying CTE
   (`WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`) has an
   ordinary `Select` root even though it deletes real rows, so it returned
   `is_valid=True`. The validator now walks the *entire* parsed tree and
   rejects an `Insert`/`Update`/`Delete`/`Merge`/`Drop`/`Create`/`Alter`/
   `TruncateTable`/`Command` node anywhere in it, not just at the root. New
   `violation_type`: `"embedded_write"` (in `SAFETY_VIOLATION_TYPES` --
   fails closed, never retried, same as `multiple_statements`).
2. **High -- closed.** Known-dangerous functions/table-valued-functions
   callable from inside an ordinary SELECT (`pg_sleep`, `pg_read_file`,
   MySQL `SLEEP`/`BENCHMARK`/`LOAD_FILE`, Oracle `UTL_HTTP.REQUEST`, MSSQL
   `OPENQUERY`/`OPENROWSET`/`OPENDATASOURCE`/`xp_cmdshell`, ...) also passed
   validation unblocked -- capable of DoS, local file disclosure, SSRF, or
   a linked-server pivot. New `violation_type`: `"dangerous_function"`
   (also in `SAFETY_VIOLATION_TYPES`), checked via an AST call-node-name
   match plus a raw-text fallback for dialect-qualified calls. Documented
   explicitly as a denylist (not exhaustive), consistent with this
   project's existing honesty about `agent/input_guard.py`'s own regex
   layer.

Additional defense-in-depth controls added in the same pass (none change
the validator allowlist itself, listed here for completeness since they're
part of the same audit): identifier quoting in `db/value_sampling.py`
(closes a second-order SQL-injection-via-malicious-identifier path),
`config/sensitive_columns.yaml`'s enforced data-classification tiers (was
previously documented in `GOVERNANCE.md` as "not yet implemented"), a
best-effort database write-privilege check (`db.connection.
check_write_privileges`), secret redaction for driver error text
(`security/redaction.py`) and a `SecretStr` wrapper for `Settings.
db_password`/`db_connection_string` (`security/secrets.py`), structured
security-event logging (`security/audit_log.py`), a RAG-poisoning
detection scan on retrieved schema context, session-scoped result caching
in `ui/app.py` (closes a process-wide `st.cache_data` cross-session leak
risk), markdown-escaping for database-sourced table/column names rendered
in the UI, and strict validation of security-relevant `.env` settings
(positive rate limits/caps/timeouts, ordered cost thresholds).

**Why:** Requested as a comprehensive enterprise-readiness security audit.
Reviewing the existing implementation first (per the audit's own
instruction) found it already unusually mature for this project's stated
scope, but the audit's discipline of testing actual payloads against the
actual code -- rather than reasoning from the design alone -- is what
surfaced the two validator gaps above; neither was previously known or
covered by `tests/test_sql_validator.py`/`tests/test_adversarial_input.py`.

**Status:** Permanent. New regression coverage:
`tests/test_sql_validator_hardening.py` (the two validator fixes),
`tests/test_value_sampling_injection.py`, `tests/test_sensitive_columns.py`,
`tests/test_write_privilege_check.py`, `tests/test_redaction.py`,
`tests/test_secrets.py`, `tests/test_audit_log.py`,
`tests/test_settings_validation.py`, `tests/test_nodes_security_wiring.py`.

---

## 2026-09-01 — Production-readiness pass: REST API surface + rate limiting/auth scope widened

**Change:** A minimal REST API (`api/`, FastAPI) was added, exposing the
same `agent.graph.run_agent` the Streamlit UI already calls. This widens
two existing control surfaces rather than introducing new ones from
scratch:

1. **Rate limiting scope widened.** `QUESTION_RATE_LIMIT_PER_MINUTE` now
   also governs a new per-client-IP `SlidingWindowRateLimiter` instance in
   `api/main.py::_limiter_for` (one limiter per source IP, mirroring the
   UI's existing per-Streamlit-session limiter) — no threshold value
   changed, but a new call site now enforces it. The existing process-wide
   `LLM_CALL_RATE_LIMIT_PER_MINUTE` limiter required no change: it already
   applies inside `generate_sql_node`, which `POST /ask` reaches through
   the same `run_agent` call as the UI.
2. **New optional auth control.** `API_AUTH_TOKEN` (`config/settings.py`,
   `api/auth.py`) is a new, off-by-default shared-bearer-token check on
   `/ask` and `/schema/tables`. Explicitly documented (`docs/API.md`) as a
   lightweight hook, not real authentication — see
   `docs/RISK_REGISTER.md`'s R-001, which this does not close.

No change to the SQL validator's allowlist, the sensitivity-classification
config, or either rate-limit *threshold* value — logged here because a new
enforcement call site and a new auth mechanism are exactly the kind of
security-relevant addition this changelog's discipline is meant to catch,
even when it widens `docs/security-changelog.md`'s originally-listed scope
(validator/classification/rate-limits/cost-thresholds) rather than falling
strictly inside it.

**Why:** Requested as part of a comprehensive production-readiness audit
(`docs/PRODUCTION_READINESS_REPORT.md`) that also found `requirements.txt`
and a prior commit message referenced a `fastapi`-based API layer that was
never actually built — this closes that specific gap for real, with the
same safety guarantees as the existing UI path, rather than leaving the
mismatch between what was claimed and what existed.

**Status:** Permanent. New regression coverage: `tests/test_api_health.py`,
`tests/test_api_ask.py`.

---

## 2026-09-10 — Multi-source RAG + web search: new secrets, new sensitivity gate, new untrusted-content surface

**Change:** An optional, off-by-default (`ENABLE_MULTI_SOURCE_ROUTER=false`)
multi-source router (`agent/orchestrator/`) was added in front of the
existing SQL pipeline, plus three new sources behind their own independent
flags — document RAG, policy RAG, and live web search
(`docs/MULTI_SOURCE_GUIDE.md`). This adds to `docs/security-changelog.md`'s
scope the same way the REST API entry above did (a new security-relevant
surface, not a change to an existing threshold value):

1. **Two new secrets**, `RAG_STORE_CONNECTION_STRING` and
   `WEB_SEARCH_API_KEY`, both `SecretStr`-wrapped identically to
   `DB_PASSWORD`/`API_AUTH_TOKEN`.
2. **A new sensitivity-classification mechanism**, `rag/store.py`'s
   `SensitivityCategory` (`compensation`, `disciplinary`, `legal`),
   extending `GOVERNANCE.md`'s "Data classification policy" from
   `(table, column)` pairs to whole policy documents/chunks — a chunk
   tagged with any of these three categories is never summarized into an
   answer, checked before generation is ever attempted, not relying on a
   prompt instruction.
3. **A new untrusted-content surface**: retrieved PDF chunk text and live
   web search results are both framed as data, never instructions, in
   their respective generation prompts (`rag/graph.py`,
   `agent.orchestrator.nodes.web_search_node`) — the same principle
   `SECURITY.md` already applies to database-sourced content, extended to
   two new input channels a malicious upload or a compromised/adversarial
   search result could exploit.
4. **One new outbound network call**: web search sends the question text
   to a third-party API (Tavily by default) — the one exception to this
   project's otherwise fully-local posture, and only when
   `ENABLE_WEB_SEARCH` is explicitly turned on.

No change to the SQL validator's allowlist, the existing column-sensitivity
config, or any existing rate-limit/cost threshold.

**Why:** Requested as part of evolving this project from a single-source
Text-to-SQL agent into a multi-source agentic RAG system (customer/sales/
financial/support-ticket data via SQL, PDF documents, sensitive HR/company
policy documents, and live web search) — each new source was given its own
safety boundary rather than treated as automatically safe because it's
read-only, per this project's own stated design principle for the work.

**Status:** Permanent, all four sources off by default. New regression
coverage: `tests/test_orchestrator.py` (router availability, LLM
classification + fallback, fan-out, synthesis attribution). `rag/` and
`search/` themselves were verified against real SQL Server 2025/Tavily/
Ollama instances during development rather than a mocked unit-test suite
— see `CLAUDE.md`'s "Known gaps" section for the follow-up this leaves.

---

## 2026-09-11 — Nested-aggregate validator check + agentic query planning's rate-limiter gap

**Change:** Two changes, both in this changelog's stated scope:

1. **New validator check, `agent/sql_validator.py`'s allowlist.** A new
   `violation_type`, `"nested_aggregate"`, rejects one aggregate function
   called inside another aggregate's own arguments (e.g. `AVG(CASE WHEN
   ... THEN SUM(x) ELSE 0 END)`) via a `sqlglot` AST walk
   (`_find_nested_aggregate`) — every supported engine
   (mssql/postgres/mysql/oracle) rejects this shape at execution time
   regardless, so this is a static, pre-execution catch, not a new
   permission being granted or withdrawn. **Not** added to
   `SAFETY_VIOLATION_TYPES` — this is an ordinary, retryable correctness
   mistake (the model gets a targeted rewrite hint and another attempt),
   not a security-gate failure. A matching execution-time backstop,
   `agent/error_classification.py`'s `ExecutionErrorCategory.
   AGGREGATE_NESTING`, catches the same failure shape by driver error text
   if the static check ever misses one (e.g. a dialect-specific aggregate
   `sqlglot` doesn't classify as `exp.AggFunc`).
2. **New, currently-uncovered LLM-call surface.** Two new graph nodes,
   `plan_query_node` and `review_sql_node` (agentic query planning +
   plan-conformance self-correction — see `docs/ARCHITECTURE.md`), make
   their own Ollama calls but do **not** check
   `agent.rate_limit.get_llm_call_limiter` themselves, unlike
   `generate_sql_node`. Both are gated behind `agent/complexity.py`'s
   signal detection (so an ordinary question is unaffected) and
   `ENABLE_QUERY_PLANNING`, but for a question that *does* trigger them,
   the realistic worst-case LLM-call count is no longer bounded solely by
   `LLM_CALL_RATE_LIMIT_PER_MINUTE` the way `SECURITY.md` previously
   described. Logged here as a known gap (see `SECURITY.md`'s "Rate
   limiting" section for the corrected worst-case math), not fixed in this
   pass — no rate-limit threshold value changed.

**Why:** The nested-aggregate check closes a reproduced real failure (a
"top 3 per year/territory with year-over-year growth" question exhausted
its entire retry budget on this exact shape, never once getting a
targeted hint that would have let it self-correct). The query-planning
feature was added to address the same underlying accuracy gap more
broadly (see `docs/PRODUCTION_READINESS_REPORT.md`'s addendum) — its
rate-limiter gap is a side effect of that addition, surfaced here rather
than left undocumented.

**Status:** Permanent (validator check). The rate-limiter gap is an open
item, not a time-boxed exception — tracked as `docs/RISK_REGISTER.md`'s
R-007. New regression coverage:
`tests/test_sql_validator.py::TestValidateSqlRejectsNestedAggregates`,
`tests/test_agent_nodes.py::TestExecuteSqlNode::
test_aggregate_nesting_error_retries_via_generate_sql`,
`tests/test_agent_nodes.py::TestPlanQueryNode`,
`tests/test_agent_nodes.py::TestReviewSqlNode`,
`tests/test_llm_client_planning.py`, `tests/test_complexity.py`.

---

## 2026-09-13 — Agentic-AI security audit: media-generation hardening + new cost/rate-limit controls

**Change:** A security audit specifically covering the multi-source
orchestrator, RAG, web search, and media generation (all added since the
2026-09-01 audit this changelog/register originally scored) found and
closed several gaps, all in change-controlled surfaces:

1. **SSRF fix, `media_gen/download.py`.** `download_media_bytes` fetched
   whatever URL IMA's API response contained with no validation at all.
   Now HTTPS-only, and the resolved address is rejected if it falls in a
   private/loopback/link-local/reserved range (`_validate_download_url`)
   — closes a real path for a compromised/malicious provider response to
   make this server fetch internal infrastructure.
2. **Human-in-the-loop approval gate for media generation,
   `REQUIRE_GENERATION_APPROVAL` (new setting, default `true`).**
   Generation is the only orchestrator source that spends real, metered
   money — it previously fired the instant the router picked it, with no
   confirmation step, unlike the SQL pipeline's "Confirm and Run" gate.
   `generation_node` now only proposes (`status="pending_approval"`, no
   provider call, no charge) until a human confirms via the new
   `POST /generate/confirm` (`api/generation.py`) or Streamlit's matching
   "Generate" button — both call the newly-extracted `execute_generation`,
   the one function that actually calls IMA.
3. **Strengthened (still heuristic) content-policy check,
   `agent.orchestrator.nodes._DISALLOWED_PROMPT_SUBSTRINGS`.** Broadened
   from 2 substrings to explicit categories (explicit/sexual content,
   minors, graphic violence, self-harm, deepfake requests). Still
   explicitly documented as a keyword heuristic, not real ML-based
   moderation — see `docs/RESPONSIBLE_AI.md`'s new media-generation
   section for the full honesty disclosure.
4. **New rate limits closing previously-unrated routes.**
   `API_ACTION_RATE_LIMIT_PER_MINUTE` (new setting) now gates `POST
   /execute`, `POST /schema/refresh`, and the mutating `/documents` routes
   (`api/rate_limit.py`, a new shared per-client-IP limiter) — none of
   these had any rate limit before. `POST /generate/confirm` gets the same
   treatment plus the existing process-wide `MEDIA_GEN_RATE_LIMIT`.
5. **Session-scoped expensive-source cost ceiling,
   `SESSION_EXPENSIVE_SOURCE_LIMIT`/`_WINDOW_SECONDS` (new settings).**
   A single question can fan out to both `generation` and `web` at once
   (observed, real router behavior); nothing previously capped how many
   times one session could keep doing that across many questions.
   `router_node` now drops those two sources from a turn's routing once a
   session's budget is spent, falling back to `sql` (see
   `docs/RISK_REGISTER.md`'s R-011 for this control's own honestly-disclosed
   limit — it's a cost control keyed on an unauthenticated correlation
   token, not an access-control boundary).
6. **Upload hardening, `api/documents.py`.** `POST /documents` previously
   read an unbounded upload into memory with no type check beyond `pypdf`
   failing to parse non-PDF content. Now capped at
   `MAX_DOCUMENT_UPLOAD_MB` (new setting, default 25) and magic-byte
   checked (`%PDF-`) before any parsing is attempted.
7. **Audit-log parity for the orchestrator.** `router_node`/`generation_node`
   previously used only plain module logging; security-relevant events
   (generation routed, rejected, rate-limited, succeeded/failed, the new
   session-ceiling trip) now also go through `security.audit_log
   .log_security_event`, matching the SQL pipeline's existing pattern.
8. **Docker hardening.** The base image is now digest-pinned (verified
   against Docker Hub's own registry API at pin time, not guessed — see
   `Dockerfile`'s own comment), and `docker-compose.yml`'s port mappings
   default to `127.0.0.1` (`APP_BIND_HOST`/`API_BIND_HOST`, new, optional
   overrides) instead of binding every host interface.
9. **CI dependency scanning, report-only.** `.github/workflows/ci.yml`
   gained a `pip-audit` step (`continue-on-error: true` — see
   `docs/RISK_REGISTER.md`'s new R-010 for why it isn't a hard gate yet).

**Why:** The new agentic-routing/RAG/media-generation surface added after
the 2026-09-01 audit had none of the authorization/approval/audit patterns
already established for the SQL pipeline — this pass closed the gaps that
were safely closeable without a larger identity/authorization-layer
rebuild (tracked separately as `docs/RISK_REGISTER.md`'s R-008, deliberately
not attempted in this pass).

**Status:** Permanent (all of the above). New regression coverage across
`tests/test_media_gen.py` (SSRF), `tests/test_orchestrator.py`
(`TestGenerationNode`'s approval-gate tests, `TestExecuteGeneration`,
`TestRouterNode`'s session-ceiling tests), `tests/test_api_documents.py`,
`tests/test_api_execute.py`, and new `tests/test_api_generation.py`/
`tests/test_api_rate_limit.py`.

---

<!--
Template for new entries — copy this block:

## YYYY-MM-DD — <short title>

**Change:** <control> changed from <old value/rule> to <new value/rule>.

**Why:** <reason>

**Status:** Permanent | Time-boxed exception (see RISK_REGISTER.md entry
dated YYYY-MM-DD, review by YYYY-MM-DD)
-->
