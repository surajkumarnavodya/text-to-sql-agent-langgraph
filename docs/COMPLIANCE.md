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
| LLM02: Insecure Output Handling | Addressed | Generated SQL is never executed without passing `agent/sql_validator.py`'s AST-based allowlist first — every time, including hand-edited SQL in the UI (`ui/app.py`'s "Confirm and Run" re-validates). Not treated as trusted output at any point. |
| LLM03: Training Data Poisoning | Not applicable | No model training/fine-tuning happens in this project; `llama3.1:8b` is used as-shipped via Ollama. |
| LLM04: Model Denial of Service | Partially addressed | `agent/rate_limit.py` (question + LLM-call limits), `LLM_MAX_TOKENS`/`INSIGHT_MAX_TOKENS` caps, `QUERY_TIMEOUT_SECONDS`, `db/query_cost.py`'s pre-execution cost gate. No circuit breaker on sustained Ollama failure (`docs/RISK_REGISTER.md`'s R-006). |
| LLM05: Supply Chain Vulnerabilities | Addressed | `requirements.txt` fully version-pinned (no unpinned/range deps); dependency-Python-version verification gap tracked as `docs/RISK_REGISTER.md`'s R-004. |
| LLM06: Sensitive Information Disclosure | Layered | `security/redaction.py` + `security.secrets.SecretStr` for connection secrets; `config/sensitive_columns.py`'s restricted-column blocking (currently unpopulated — R-002); results/errors never logged with cell values (`observability/redaction.py`). |
| LLM07: Insecure Plugin Design | Not applicable | No plugin/tool-calling architecture — the agent's only "tool" is the SQL execution path, itself gated by the validator. |
| LLM08: Excessive Agency | Addressed by design | The agent never executes SQL the user hasn't implicitly approved by asking the question, and the UI additionally gates *displayed* execution behind "Confirm and Run" (`CLAUDE.md`'s "SQL is untrusted output, always"). Retries are capped (`MAX_RETRIES`) and every attempt is logged/shown, not silently expanded. |
| LLM09: Overreliance | Partially addressed | The UI shows generated SQL, a retry timeline, and (this audit's finding) the real accuracy numbers are now documented (`docs/EVALUATION.md`) rather than only implied by feature-list language — a user reading `docs/EVALUATION.md` before trusting an answer is the actual mitigation; nothing in the UI itself warns per-answer. |
| LLM10: Model Theft | Not applicable | Fully local model via Ollama; no hosted model API key or proprietary model artifact to steal. |

## OWASP API Security Top 10 (relevant to `api/`, added this pass)

| Risk | Status | Where |
|---|---|---|
| API1: Broken Object Level Authorization | Not applicable | No per-object/per-user data model — every authorized caller sees the same database, same as the single-user UI. |
| API2: Broken Authentication | Documented gap | `api/auth.py`'s optional shared-token hook is not real authentication (`docs/API.md`). Deployment guidance requires a reverse proxy for anything beyond trusted-network use. |
| API3: Broken Object Property Level Authorization | Not applicable | Same reasoning as API1. |
| API4: Unrestricted Resource Consumption | Addressed | Per-IP question rate limit (`api/main.py::_limiter_for`) + the existing process-wide LLM-call limiter, row cap, query timeout, cost-estimation gate — all shared with the UI path since `/ask` calls the same `run_agent`. |
| API5: Broken Function Level Authorization | Not applicable | No differentiated roles/functions to gate beyond the single shared token. |
| API6: Unrestricted Access to Sensitive Business Flows | Partially addressed | Rate limiting covers volumetric abuse; no CAPTCHA-style human-verification layer (judged disproportionate for this project's scale). |
| API7: Server Side Request Forgery | Addressed | The SQL validator's dangerous-function denylist blocks SSRF-capable SQL constructs (`OPENROWSET`, `UTL_HTTP.REQUEST`, etc. — see `agent/sql_validator.py::_DANGEROUS_FUNCTION_NAMES`); the API itself makes no user-controlled outbound HTTP calls. |
| API8: Security Misconfiguration | Partially addressed | Secrets never in source (`.env` gitignored), `Dockerfile` runs non-root, pinned deps. `docs/PRODUCTION_CHECKLIST.md` exists specifically to catch per-deployment misconfiguration (e.g. the DB-role least-privilege gap this audit found in the reference environment). |
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
- **Manage:** Retry/error-feedback loop bounded (`MAX_RETRIES`), fail-open
  design for non-critical checks (cost estimation, write-privilege check)
  vs. fail-closed for the safety-critical one (SQL validator), rate
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
  results are shown (`ui/app.py`'s "Confirm and Run" gate) — the system
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
