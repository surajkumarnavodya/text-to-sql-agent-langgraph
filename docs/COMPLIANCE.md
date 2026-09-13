# Compliance

A self-assessment of how this project's actual, verified controls map
against four named external frameworks — **not** a claim of certification,
audit, or formal compliance against any of them. This is a solo-maintainer
project (`docs/GOVERNANCE.md`); "compliance" here means "here's how the
real, code-verified controls line up against a recognized framework's
categories," useful for a reader evaluating this project against a
checklist they already trust, not an assertion this project has been
formally assessed by anyone but its own author. Every control cited below
was verified against the running code as part of the 2026-09-01
production-readiness audit (`docs/PRODUCTION_READINESS_REPORT.md`), not
just read off a design doc.

## OWASP Top 10 for LLM Applications

| Risk | Status | Where |
|---|---|---|
| LLM01: Prompt Injection | Layered, not eliminated | `agent/input_guard.py` (pre-filter regex), the system prompt's untrusted-data framing (`agent/llm_client.py`), and — the actual backstop — `agent/sql_validator.py`'s SELECT-only allowlist plus a read-only DB role. `SECURITY.md`'s "What is explicitly not guaranteed" is explicit that the regex layer is beatable; the SQL validator is what bounds the consequences. |
| LLM02: Insecure Output Handling | Addressed | Generated SQL is never executed without passing `agent/sql_validator.py`'s AST-based allowlist first — every time, including hand-edited SQL in the dashboard ("Confirm and Run" re-validates via `POST /execute`). Not treated as trusted output at any point. |
| LLM03: Training Data Poisoning | Not applicable | No model training/fine-tuning happens in this project; `llama3.1:8b` is used as-shipped via Ollama. |
| LLM04: Model Denial of Service | Partially addressed | `agent/rate_limit.py` (question + LLM-call limits), `LLM_MAX_TOKENS`/`INSIGHT_MAX_TOKENS`/`QUERY_PLAN_MAX_TOKENS`/`SQL_REVIEW_MAX_TOKENS` caps, `QUERY_TIMEOUT_SECONDS`, `db/query_cost.py`'s pre-execution cost gate. The process-wide LLM-call limiter covers `generate_sql_node` but not the newer `plan_query_node`/`review_sql_node` calls (`docs/RISK_REGISTER.md`'s R-007) or sustained Ollama failure generally (R-006). **2026-09-13 addition:** media generation (a real, metered third-party call, not an LLM call, but the same "denial of wallet" risk class) has its own process-wide limiter (`MEDIA_GEN_RATE_LIMIT`) plus a new session-scoped ceiling on combined generation/web invocations (`SESSION_EXPENSIVE_SOURCE_LIMIT` — R-011 discloses this control's own honest limit); `/execute`, `/schema/refresh`, and the mutating `/documents` routes gained rate limiting they previously lacked entirely (`API_ACTION_RATE_LIMIT_PER_MINUTE`, `api/rate_limit.py`). |
| LLM05: Supply Chain Vulnerabilities | Addressed | `requirements.txt` fully version-pinned (no unpinned/range deps); dependency-Python-version verification gap tracked as `docs/RISK_REGISTER.md`'s R-004. |
| LLM06: Sensitive Information Disclosure | Layered | `security/redaction.py` + `security.secrets.SecretStr` for connection secrets (including `IMA_API_KEY`/`WEB_SEARCH_API_KEY` as of the 2026-09-10/13 additions); `config/sensitive_columns.py`'s restricted-column blocking (currently unpopulated — R-002); results/errors never logged with cell values (`observability/redaction.py`). Generated media's provider CDN URL is never exposed to the LLM, the API response, or the client — only an opaque `media_id`, served through this app's own `GET /media/{media_id}` (`media_gen/cache.py`). |
| LLM07: Insecure Plugin Design | Partially addressed (2026-09-13 revision) | The agent *does* have a tool/source-routing architecture as of the multi-source orchestrator (`agent/orchestrator/`) — this row was "Not applicable" before that existed. Mitigations: source descriptions fed to the router's classifier are hardcoded Python constants, not user-editable/dynamically-loaded config (no injectable "malicious tool description" surface); there is no MCP client/server anywhere in this codebase (confirmed by repo-wide search during the 2026-09-13 audit); tool inputs/outputs are validated per-source (SQL through the existing validator, media-generation URLs through the new SSRF check in `media_gen/download.py`). **Gap:** no independent authorization layer sits between the router's LLM classification and actual execution (`docs/RISK_REGISTER.md`'s R-008) — the classifier's output is trusted directly, gated only by global config flags, not per-request/per-user policy. |
| LLM08: Excessive Agency | Addressed for SQL; partial for media generation | The agent never executes SQL the user hasn't implicitly approved by asking the question, and the UI additionally gates *displayed* execution behind "Confirm and Run" (`CLAUDE.md`'s "SQL is untrusted output, always"). Retries are capped by a bounded, per-question budget (`MAX_RETRIES`, adaptively widened up to `COMPLEX_QUERY_MAX_RETRY_BONUS` by `agent/complexity.py` — never unbounded) and every attempt is logged/shown, not silently expanded — this now includes the agentic query-planning + plan-conformance review pass (`plan_query_node`/`review_sql_node`), which shares the same bounded budget rather than adding a second, independent loop. **2026-09-13 addition:** media generation — the one orchestrator source that spends real, metered money — previously fired autonomously the instant the router picked it; it now requires an explicit human confirmation (`REQUIRE_GENERATION_APPROVAL`, default `true`, `POST /generate/confirm`) before any provider call, mirroring "Confirm and Run." The router's fan-out to *which* source(s) run at all is still LLM-decided with no independent policy layer (see LLM07/R-008 above). |
| LLM09: Overreliance | Partially addressed | The UI shows generated SQL, a retry timeline, and (this audit's finding) the real accuracy numbers are now documented (`docs/EVALUATION.md`) rather than only implied by feature-list language — a user reading `docs/EVALUATION.md` before trusting an answer is the actual mitigation; nothing in the UI itself warns per-answer. |
| LLM10: Model Theft | Not applicable | Fully local model via Ollama; no hosted model API key or proprietary model artifact to steal. |

## OWASP API Security Top 10 (relevant to `api/`, added this pass)

| Risk | Status | Where |
|---|---|---|
| API1: Broken Object Level Authorization | Not applicable | No per-object/per-user data model — every authorized caller sees the same database, same as the single-user UI. |
| API2: Broken Authentication | Documented gap | `api/auth.py`'s optional shared-token hook is not real authentication (`docs/API.md`). Deployment guidance requires a reverse proxy for anything beyond trusted-network use. |
| API3: Broken Object Property Level Authorization | Not applicable | Same reasoning as API1. |
| API4: Unrestricted Resource Consumption | Addressed | Per-IP question rate limit (`api/main.py::_limiter_for`) + the existing process-wide LLM-call limiter, row cap, query timeout, cost-estimation gate — all shared with the UI path since `/ask` calls the same `run_agent`. **2026-09-13 addition:** `POST /execute`, `POST /schema/refresh`, and the mutating `/documents` routes previously had no rate limit of their own at all — now covered by a shared per-client-IP limiter (`api/rate_limit.py`, `API_ACTION_RATE_LIMIT_PER_MINUTE`); `POST /documents` also gained an upload size cap (`MAX_DOCUMENT_UPLOAD_MB`) where previously the entire file was read into memory unbounded. |
| API5: Broken Function Level Authorization | Not applicable | No differentiated roles/functions to gate beyond the single shared token. |
| API6: Unrestricted Access to Sensitive Business Flows | Partially addressed | Rate limiting covers volumetric abuse; no CAPTCHA-style human-verification layer (judged disproportionate for this project's scale). |
| API7: Server Side Request Forgery | Addressed | The SQL validator's dangerous-function denylist blocks SSRF-capable SQL constructs (`OPENROWSET`, `UTL_HTTP.REQUEST`, etc. — see `agent/sql_validator.py::_DANGEROUS_FUNCTION_NAMES`). **2026-09-13 revision:** this row was previously "the API itself makes no user-controlled outbound HTTP calls," which stopped being true once media generation was added — `media_gen/download.py` fetches whatever URL IMA's API response contains, server-side. Found during the 2026-09-13 audit and fixed the same day: HTTPS-only, and the resolved address is rejected if it falls in a private/loopback/link-local/reserved range (`_validate_download_url`). `search/web_search.py` was checked and confirmed to only ever call Tavily's own fixed endpoint, never an arbitrary URL. |
| API8: Security Misconfiguration | Partially addressed | Secrets never in source (`.env` gitignored), `Dockerfile` runs non-root, pinned deps. `docs/PRODUCTION_CHECKLIST.md` exists specifically to catch per-deployment misconfiguration (e.g. the DB-role least-privilege gap this audit found in the reference environment). **2026-09-13 additions:** the base image is now digest-pinned (was a floating tag), `docker-compose.yml`'s port mappings now default to `127.0.0.1` (was an unqualified mapping reachable from the whole host network), and CI gained a report-only dependency-vulnerability scan (`pip-audit`, R-010) — none of these three were true before this pass. |
| API9: Improper Inventory Management | Addressed | `docs/API.md` documents both current endpoints; no undocumented/shadow endpoints. |
| API10: Unsafe Consumption of APIs | Not applicable | The API doesn't consume third-party APIs beyond Ollama (local, trusted) and the configured database. |

## NIST AI Risk Management Framework (self-assessment)

Mapped against the four functions, at the granularity appropriate for a
solo-maintainer project, not a full RMF profile:

- **Govern:** `docs/GOVERNANCE.md` states ownership, data-classification
  policy, change control for security-relevant changes, and an exception
  process — all real, all cross-referenced from this document. Single
  point of accountability is explicit, not implied.
- **Map:** This project's context (local, single-user-oriented, real
  customer database, LLM-generated SQL as the core risk surface) is
  documented in `SECURITY.md`, `README.md`'s "Known limitations," and
  `docs/RESPONSIBLE_AI.md`. Known risks are enumerated in
  `docs/RISK_REGISTER.md`, not left implicit.
- **Measure:** `eval/` is a real, execution-accuracy-based benchmark
  harness (not a self-reported claim) with a committed baseline
  (`eval/baselines/latest.json`) and regression detection
  (`eval/regression.py`). `docs/EVALUATION.md` reports the actual latest
  numbers, including the ones that look bad (35% final accuracy), rather
  than only the ones that look good (100% security-rejection accuracy).
- **Manage:** Retry/error-feedback loop bounded (`MAX_RETRIES`, adaptively
  widened per question by `agent/complexity.py` but still capped, never
  unbounded — the agentic query-planning + plan-conformance review pass
  shares this same bounded budget), fail-open design for non-critical
  checks (cost estimation, write-privilege check, query planning, plan
  review) vs. fail-closed for the safety-critical one (SQL validator), rate
  limiting, and a documented risk register with review dates. Residual
  risk (no auth, unclassified sensitive columns, low accuracy) is named,
  not hidden.

## ISO/IEC 42001 (AI Management System) — self-assessment

ISO/IEC 42001 expects an organizational AI management system; a
solo-maintainer project doesn't have "an organization" in the sense the
standard assumes, so this is a mapping of intent, not a claim of
certifiable conformance:

- **AI policy** — `docs/GOVERNANCE.md` + `docs/RESPONSIBLE_AI.md` serve
  this role at solo-project scale.
- **Roles and responsibilities** — single-maintainer, explicitly stated,
  not distributed across an org (`GOVERNANCE.md`'s "Ownership").
- **Risk assessment** — `docs/RISK_REGISTER.md`, seeded from a real audit,
  not a template filled with hypotheticals.
- **Data for AI systems** — `docs/GOVERNANCE.md`'s data classification
  policy + `db/value_sampling.py`'s privacy-by-construction cardinality
  cap. Currently unpopulated classification is disclosed (R-002), not
  hidden behind the policy's existence.
- **Third-party/supplier relationships** — Ollama (local, no data
  leaves the machine) and the four supported DB drivers, all pinned,
  all open-source with public repos.

## EU AI Act — self-assessment

This is a **general-purpose Text-to-SQL assistant against a database the
operator already has full read access to** — not a system making
consequential automated decisions about individuals (credit, employment,
law enforcement, etc.), and not embedded in a product category the Act
lists as high-risk. Under the Act's risk-tiering, this most plausibly sits
outside the high-risk categories entirely, closer to a limited-risk/general
AI-system profile:

- **Transparency:** Users see the generated SQL and can review it before
  results are shown (the dashboard's "Confirm and Run" gate) — the system
  does not present LLM output as ground truth without a human-visible,
  human-editable intermediate artifact.
- **Human oversight:** The manual confirmation step is exactly this — no
  fully automated action taken on generated SQL without it passing through
  a point where a human could stop or edit it.
- **Accuracy disclosure:** `docs/EVALUATION.md` states real, measured
  accuracy rather than an unverified marketing claim — relevant to the
  Act's transparency expectations even outside the high-risk tier.

**Not assessed:** this is not legal advice, and no one has run a formal
Act conformity assessment against this project. If you're deploying this
in an EU context for anything beyond personal/internal use, get real legal
review — this section exists so you know where to start that conversation,
not to substitute for it.

## Cross-references

- [`../SECURITY.md`](../SECURITY.md) — the technical controls this
  document maps, in full detail.
- [`GOVERNANCE.md`](GOVERNANCE.md) — ownership, change control, review
  cadence.
- [`RESPONSIBLE_AI.md`](RESPONSIBLE_AI.md) — design choices through a
  responsible-AI lens.
- [`RISK_REGISTER.md`](RISK_REGISTER.md) — every named gap above, with
  severity and a review date.
- [`PRODUCTION_READINESS_REPORT.md`](PRODUCTION_READINESS_REPORT.md) — the
  audit this document's findings are drawn from.
