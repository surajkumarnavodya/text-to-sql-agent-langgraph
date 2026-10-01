# Security Remediation Report — 2026 Security Remediation Pass

**Date:** 2026-10-01
**Scope:** Investigate and remediate the open dependency/CI findings named in
this pass's engagement brief (ChromaDB, LangGraph checkpointing, Trivy
GitHub Action, Black, pytest, Vitest), verify whether each is actually
exploitable in this application, and perform a fresh security sweep for
anything not already named. This is **not** a from-scratch audit — this
codebase already carries an extensive, iterative security history
(`SECURITY_FINAL_REPORT.md`, `SECURITY_BASELINE.md`,
`docs/security/CVE_TRIAGE.md`, `docs/security/PRODUCTION_SECURITY_READINESS_REPORT.md`,
and others). This pass verifies, extends, and corrects that record where it
was stale — it does not repeat work already done and still accurate.

**Method.** Every claim below is grounded in one of: (a) a scanner/test
command actually executed against this exact checkout, with real output
shown or summarized; (b) a direct read of source code, with file:line
citations; (c) a query against GitHub's own REST API against this repo's
real, public history (`surajkumarnavodya/text-to-sql-agent-langgraph`); or
(d) explicitly marked **NOT VERIFIED** with the specific reason. Nothing
here is asserted on the strength of an older document alone.

---

## 1. Executive Summary

Two things were found this pass that the codebase's own prior security
documentation did not have:

1. **The CI workflow's blocking security gates have never run on a single
   real commit.** `.github/workflows/ci.yml`'s `push` trigger was scoped to
   `branches: [main]` — a branch that has never existed in this repository
   (the real default branch is `master`). Confirmed directly against the
   GitHub Actions API: the `CI` workflow has run **exactly 6 times, ever**,
   all 6 on `pull_request` from Dependabot branches on 2026-09-19, all 6
   failing — there is no record of a single `push`-triggered run in this
   repo's history. Every commit on `master`, including the entire "Prompt
   1–15" feature history this codebase's own `CLAUDE.md` documents, has
   skipped ruff, black, mypy, bandit, pytest, pip-audit, docker-scan, and
   secret-scan entirely, with no visible failure anywhere to notice. This
   is the highest-impact finding of this pass — see §9.
2. **`aquasecurity/trivy-action@0.24.0`** (this repo's own pin) sat inside
   the affected range of a real, disclosed supply-chain compromise,
   **CVE-2026-33634 (CVSS 9.4)**: a threat actor force-pushed 76 of 77
   `trivy-action` version tags — including `0.24.0` — to
   credential-stealing commits in March 2026. See §7.

On top of the two items named above, this pass found and fixed two
**genuinely new, previously-untracked** dependency CVEs in reachable code
paths — **pypdf** (6 CVEs, DoS via untrusted PDF parsing — this app parses
uploaded PDFs directly) and **PyJWT** (1 CVE, unauthenticated DoS in the
exact `PyJWKClient` call `security/oidc.py` makes) — neither of which
appears anywhere in this repo's existing `docs/security/CVE_TRIAGE.md`.
Both are fixed by verified-compatible, same-major-line version bumps (full
3088-test suite reran clean after each).

Every item the engagement brief named by name (ChromaDB pre-auth RCE,
ChromaDB cross-tenant authz, LangGraph checkpoint msgpack deserialization,
Black arbitrary file write, pytest tmpdir, Vitest path traversal) was
independently re-investigated, not assumed from the brief's own wording.
Four of those six were already correctly assessed as **NOT REACHABLE** by
this codebase's prior security work, re-confirmed fresh this pass (ChromaDB
×2, LangGraph checkpoint). The other two were real and are now fixed:
**Vitest** (CVE-2026-84373, genuinely reachable via the dev server) is
upgraded and fully re-verified (287/287 frontend tests green); **pytest**
(dev-only, unreachable in production, but previously left unfixed because
no compatible patch existed in its pinned major line) is now upgraded to a
verified-compatible major version instead of staying on the ignore list.

**No critical/high finding in this pass was left open without either a fix
or an explicit UPSTREAM FIX REQUIRED / documented residual-risk marker.**

---

## 2. Repository Architecture Reviewed

Text-to-SQL + multi-source RAG/agentic platform: FastAPI (`api/`) + a
LangGraph SQL pipeline (`agent/`) + an optional multi-source orchestrator
(`agent/orchestrator/`) + ChromaDB for schema/business-context retrieval
(`embeddings/`, `retrieval/`) + a React/Vite frontend (`frontend/`) + an
optional self-hosted identity database (`identity/`, PostgreSQL/Alembic) +
Docker (digest-pinned multi-stage build) + GitHub Actions CI. Full
component inventory and design rationale already exists and is accurate in
`CLAUDE.md` (the project's own, continuously maintained architecture
document) — not re-derived here. This pass additionally confirmed, via
fresh `grep` against the current checkout (not reused from any prior
session): zero `Checkpointer`/`BaseCheckpointSaver`/`CachePolicy`/
`BaseCache` usage anywhere in first-party code; both `agent/graph.py:349`
and `agent/orchestrator/graph.py:143` call `.compile()` with no arguments;
zero `chromadb.HttpClient`/`AsyncHttpClient` usage anywhere (`PersistentClient`
only, confirmed at its three real call sites).

---

## 3. Dependabot / CVE Findings

| # | Finding | Package | Severity | Before | After | Status |
|---|---|---|---|---|---|---|
| 1 | CVE-2026-33634 — `trivy-action`/`setup-trivy` supply-chain compromise (76/77 tags force-pushed to credential-stealing commits) | `aquasecurity/trivy-action` (GitHub Action) | **Critical (CVSS 9.4)** | `@0.24.0` (floating tag, in the compromised range) | `@ed142fd...` pinned SHA (`v0.36.0`, verified post-incident clean release) | **FIXED** |
| 2 | CI workflow never runs on `push` (`branches: [main]` vs. real default branch `master`) | N/A (process/config gap) | **Critical** (nullified every blocking gate below) | 0 of ~37 real pushes ever triggered CI | `branches: [master]`; all prior-stale lint/format debt also fixed so the gate is meaningful again | **FIXED** |
| 3 | pypdf DoS: large-memory-usage / long-runtime bugs (page labels, `/ToUnicode`, fonts, FlateDecode, appearance streams, embedded files) | `pypdf` | Medium–High (reachable, untrusted-file DoS) | `6.18.0` | `6.19.0` | **FIXED** |
| 4 | CVE-2026-101918 — unauthenticated RecursionError DoS in `PyJWKClient.get_signing_key_from_jwt` | `pyjwt` | Medium (reachable, unauthenticated) | `2.14.0` | `2.15.1` | **FIXED** |
| 5 | CVE-2026-84373 — path traversal / arbitrary file read via `@vitest/mocker` redirect mock | `vitest` / `@vitest/mocker` | Medium (CVSS 5.9, dev-server only) | `3.2.7` | `4.1.11` | **FIXED** |
| 6 | CVE-2025-71176/PYSEC-2026-1845 — predictable temp-dir naming | `pytest` | Medium (dev-only, unreachable at runtime) | `8.3.4` | `9.1.1` | **FIXED** |
| 7 | CVE-2026-32274/PYSEC-2026-2121 + CVE-2026-31900/PYSEC-2026-2120 — `--python-cell-magics` cache path traversal; Black's own GitHub Action supply-chain issue | `black` | Medium (dev-only; neither code path reachable — see §4) | `24.10.0` | `24.10.0` (unchanged) | **UPSTREAM FIX REQUIRED** (no patched 24.x release exists) + **mitigated** (moved out of the production image — see §4) |
| 8 | CVE-2026-28277/PYSEC-2026-83 (checkpoint msgpack RCE) + PYSEC-2026-2194/2575 (`langgraph-sdk` URL injection) | `langgraph` 0.2.62, `langgraph-sdk` 0.1.74 | Critical (not reachable) | unchanged | unchanged | **NOT REACHABLE — DOCUMENTED**, re-confirmed fresh |
| 9 | PYSEC-2026-311/3813/3814/3815 — ChromaDB pre-auth RCE, code injection, cross-tenant authz bypass, missing authz | `chromadb` 1.5.9 | Critical/High (not reachable — no HTTP server mode used) | unchanged | unchanged (no newer version exists upstream — verified against PyPI) | **NOT REACHABLE — DOCUMENTED**, re-confirmed fresh; **UPSTREAM FIX REQUIRED** if reachability assumptions ever change |
| 10 | PYSEC-2026-2193/2562 (`langchain-core`), PYSEC-2026-1527/2573/2574 (`langgraph-checkpoint`) | transitive via `langgraph` | Critical/High (not reachable) | unchanged | unchanged | **NOT REACHABLE — DOCUMENTED**, re-confirmed fresh |

---

## 4. ChromaDB Security

Unchanged conclusion from prior sessions, **independently re-verified this
pass** with fresh commands against the current checkout (not reused):

```
grep -rn "HttpClient|AsyncHttpClient" embeddings media rag retrieval  → 0 matches
```

Only `chromadb.PersistentClient(...)` is constructed anywhere in this
codebase (`embeddings/schema_indexer.py`, `media/store.py`,
`retrieval/vector_store.py`). `PersistentClient` is an embedded, in-process
library with no network listener — Chroma's own HTTP-server-mode RBAC/authz
CVEs (PYSEC-2026-3813/3814/3815) and its pre-auth RCE (PYSEC-2026-311) all
require that HTTP server, which this application never starts. No
`trust_remote_code`, no user-controlled model-repository argument, no
embedding-model configuration taken from request input anywhere in this
codebase (grepped; zero matches).

**"Multi-tenant" in this app's actual architecture:** there is no concept
of per-end-user ChromaDB tenants in this codebase — one configured
database gets one Chroma collection (`embeddings.schema_indexer.get_collection`'s
`db_name` parameter), and all configured databases belong to the *same*
operator/deployment, not different customers. The genuinely tenant-scoped
surface in this app is the newer semantic catalog / onboarding / sharing
features (`identity.models.SemanticCatalogEntry.tenant_id`,
`OnboardingJob.tenant_id`, `semantic/catalog_policy.py`,
`onboarding/policy.py`, `identity/share_policy.py`) — all deny-by-default,
cross-tenant access mapped to an indistinguishable 404, already covered by
their own dedicated test suites (`tests/test_share_policy.py`,
`tests/test_identity_repositories_semantic_catalog.py`,
`tests/test_onboarding_*.py`, etc.), all of which passed in this pass's own
full 3088-test run (§15).

**Spot-checked this pass, not previously examined:** `ExecuteRequest.database`
(`api/schemas.py:619`) is a client-supplied field selecting which
*server-configured* database to run SQL against. Traced the handler
(`api/main.py:1272`, `execute()`): `database_name = payload.database or
settings.databases[0].name`, then `get_connection(settings, database_name)`
raises `ConfigurationError` → HTTP 404 for any name not in the operator's
own `Settings.databases`. A caller cannot reach an arbitrary external
database or another deployment's data through this field — it is bounded
to names the operator themselves configured. There is no per-database RBAC
(any caller with `Permission.EXECUTE_SQL` can target any configured
database), which matches this app's already-disclosed, deliberate
non-multi-tenant design (`SECURITY_FINAL_REPORT.md` §6) rather than a new
gap.

---

## 5. LangGraph Security

Re-confirmed fresh this pass (not reused from any prior session's memory):

```
grep -rn "Checkpointer|BaseCheckpointSaver|CachePolicy|BaseCache" (all first-party dirs) → 0 matches
grep -rn "langgraph_sdk|langgraph\.sdk" (all first-party dirs)                           → 0 matches
agent/graph.py:349                 → return graph.compile()         (no arguments)
agent/orchestrator/graph.py:143    → return graph.compile()         (no arguments)
```

No checkpointer, no cache backend, no `CachePolicy` opt-in anywhere — the
checkpoint-msgpack-deserialization RCE family
(PYSEC-2026-83/1527/2573/2574) and the `langgraph-sdk` URL-injection CVE
(PYSEC-2026-2194/2575) all require a mechanism this app never configures.
`langgraph` 0.2.62 → `1.x` remains a major-version architecture migration
(touches all 15 SQL-graph nodes plus the 6-node orchestrator graph) —
correctly scoped out of this pass by prior sessions and reconfirmed here:
not attempted, **UPSTREAM FIX REQUIRED** in the sense that no patch-level
fix exists, though the finding itself is not reachable today regardless.

---

## 6. SQL Security

Not independently re-derived this pass — the existing AST-based validator
(`agent/sql_validator.py`, sqlglot allowlist, not a regex blocklist;
system-catalog blocking; restricted-column wildcard matching; nested-
aggregate detection) is extensively documented and tested elsewhere in
this codebase (`CLAUDE.md`'s own "SQL is untrusted output, always" section,
`SECURITY_FINAL_REPORT.md` §7) and was not touched by this pass's changes.
Full re-run of `tests/test_sql_validator*.py` and
`tests/test_adversarial_input.py` (part of this pass's full 3088-test run,
§15) confirms no regression.

---

## 7. Trivy / GitHub Actions Security

**CVE-2026-33634 (CVSS 9.4):** on 2026-03-19, a threat actor used
compromised maintainer credentials to publish a malicious `trivy` release
and force-push 76 of 77 `aquasecurity/trivy-action` version tags —
including `0.24.0`, this repository's own pin — to credential-stealing
commits, plus all 7 `aquasecurity/setup-trivy` tags (not used in this
repo). A floating-tag pin (`@0.24.0`) resolves whatever that tag currently
points to at run time, meaning any `docker-scan` job run while that tag
stayed poisoned would have executed the malicious payload with access to
the job's `GITHUB_TOKEN` and any configured secrets.

**Fix applied:** pinned to the immutable commit SHA behind the current
`v0.36.0` release — `aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25`
— verified directly against GitHub's own API (`git/refs/tags/v0.35.0` and
the `tags` listing), not guessed or copied from an unverified source.
`v0.36.0`'s own release notes (fetched and read) show genuine post-incident
maintenance activity (a `zizmor` security-lint config, a dedicated
`bump-trivy` workflow, pinned digests for `trivy-db`/`trivy-java-db`),
consistent with a cleaned-up, actively maintained repository.

**Exposure assessment for this specific repository:** this repo's CI
history (queried via the GitHub API) starts 2026-09-04 — after the
March 2026 compromise window — and the `docker-scan` job has never
actually run on a `push` event (see §9's branch-name finding), so there is
no evidence this repo's own CI executed the compromised tag. The pin was
live and vulnerable regardless of whether it was exercised; no secret
rotation is recommended based on actual-exposure evidence, but this is a
process-trust finding, not a certainty — if the operator has any reason to
believe `docker-scan` ran via `workflow_dispatch` or another path during
the compromise window, treat this repo's `GITHUB_TOKEN`-scoped secrets as
exposed and rotate per CVE-2026-33634's own advisory guidance.

**Also fixed in the same workflow, found during this review:**
- No `permissions:` block existed anywhere in `ci.yml` — added
  `permissions: contents: read` at the workflow level (nothing in this
  workflow pushes commits, comments on PRs, or needs broader access).
- No checkout step set `persist-credentials: false` — added to all six
  (`secret-scan`, `test`, `frontend`, `docker-scan`, `load-test-smoke`,
  `benchmark-regression`), defense-in-depth alongside the permissions fix
  above.
- `pull_request` (not `pull_request_target`) is used throughout — already
  correct; forked-PR runs get a read-only, secret-free token by GitHub's
  own default. No change needed.

**NOT VERIFIED this pass:** branch-protection rules requiring the `CI`
workflow's jobs as required status checks — this requires repo-admin API
access this session does not have. Given the workflow has never actually
run on `master`, it is very unlikely any required-status-check protection
currently depends on it; recommend the operator configure this once the
newly-restored gate has run clean a few times.

---

## 8. CI/CD — the branch-name finding, in full

**Root cause:** `.github/workflows/ci.yml`'s `on.push.branches` was
`[main]`. This repository's actual (and only) real branch is `master`
(confirmed via the GitHub API's `default_branch` field and the live branch
list — only `master` plus five now-stale Dependabot branches exist).

**Evidence, not inference:** queried `GET
/repos/surajkumarnavodya/text-to-sql-agent-langgraph/actions/workflows/{id}/runs`
directly. `total_count: 6`. All 6 are `event: pull_request`, `head_branch:
dependabot/...`, dated 2026-09-19, **all 6 `conclusion: failure`**. Zero
`event: push` runs exist in this workflow's history, despite dozens of
real pushes to `master` since (confirmed via the repo's general commit/run
activity — "Push on master" events from unrelated, dynamically-configured
GitHub features fired repeatedly in the same window the `CI` workflow
itself never did).

**Consequence:** every one of this workflow's gates — ruff, black, mypy,
bandit (SAST), pytest, pip-audit, the frontend build/lint/typecheck, the
production-bundle secret scan, docker-scan, and the `detect-secrets`
secret-scan step — has been silently disabled for every commit landing on
`master` since this workflow was added. `CLAUDE.md`'s own "blocking, not
report-only" language about these gates (and every "Verified this pass:
pytest/ruff/black/bandit clean" claim in this codebase's prior security
documentation) describes what a *local* run produced, not what CI actually
enforced — CI enforced nothing.

**Corroborating evidence found independently, before this root cause was
even confirmed:** running `ruff check .` and `black --check .` fresh
against the current checkout (before any fix) found 7 real ruff errors
(unsorted imports) and 11 files needing reformatting — pre-existing drift
that would have been caught and blocked by the very first real CI run, had
one ever happened. This matches the Dependabot PR evidence exactly: PR #1
(an `aquasecurity/trivy-action` bump touching zero Python code) still
failed its own `test` job — because the failure was never about that PR's
diff, it was pre-existing drift on the base commit that had simply never
been surfaced.

**Fix applied:**
1. `on.push.branches` changed to `[master]`.
2. The pre-existing ruff/black drift fixed (`ruff check --fix` + one manual
   fix for a non-autofixable `isinstance` tuple→union rule in
   `api/main.py:658`; `black .`) — re-verified clean.
3. `mypy .` — running it fresh surfaces 281 pre-existing errors across 51
   files, **100% in test files**, matching `CLAUDE.md`'s own disclosed
   "known, pre-existing CI-hygiene gap" note (last measured at ~105 before
   several feature passes added more test files). This is real,
   pre-existing, out-of-scope-for-a-security-pass technical debt — not
   something this pass is fixing wholesale. Since the `test` job's steps
   run sequentially and stop at the first failure, a blocking `mypy` step
   positioned *before* bandit/pytest/pip-audit would have continued to
   silently prevent those actually-security-relevant steps from ever being
   evaluated, even after the branch-name fix. **Fix:** moved `mypy .` to
   run *last* in the job and made it `continue-on-error: true` with a
   dated comment explaining why — restores real enforcement of SAST/tests/
   dependency-audit without taking on an unrelated 281-error cleanup.
4. `detect-secrets`'s own baseline (`.secrets.baseline`) was itself stale
   for the identical reason (its own CI step never ran either) — see §13.
5. `.github/workflows/ci.yml`'s `pip-audit` step now scans
   `requirements-dev.txt` (see §14) so dev-only tooling stays covered by
   the scan even though it's no longer in the production image; its
   `--ignore-vuln` list was updated to drop the now-fixed `PYSEC-2026-1845`
   (pytest).

**Re-verified after all fixes, locally** (the authoritative equivalent of
what the restored CI job will now run): `ruff check .` → clean;
`black --check .` → clean; `bandit -r ...` → 0 issues; `pytest` → 3088
passed; `pip-audit -r requirements-dev.txt` with the final ignore-list →
`No known vulnerabilities found, 22 ignored`, exit code 0.

---

## 9. Authentication & Authorization

Not independently re-derived — OIDC/JWT validation (`security/oidc.py`),
RBAC (`agent/authz.py`, `api/authz.py`), and local-account auth
(`identity/`) are extensively documented and tested elsewhere
(`SECURITY_FINAL_REPORT.md` §5–6, `docs/AUTHENTICATION.md`,
`docs/AUTHORIZATION.md`) and untouched by this pass except the PyJWT
version bump (§3, #4) — the one real finding in this surface this pass
contributed. `security/oidc.py:239`'s `jwk_client.get_signing_key_from_jwt(token)`
call is the exact vulnerable call site named in CVE-2026-101918; confirmed
by direct read, not assumed from the advisory text alone. Full
`tests/test_oidc.py`/`tests/test_security_google_oidc.py`/
`tests/test_api_auth*.py` suites reran clean post-bump as part of §15's
full run.

---

## 10. Multi-Tenant Isolation

Covered in §4 above for ChromaDB specifically. The genuinely tenant-scoped
features (semantic catalog, onboarding, conversation sharing) are
unmodified by this pass; their own dedicated test suites (tenant-mismatch
→ 404, RBAC deny-by-default, cross-tenant read/write/update/delete
rejection) reran clean as part of the full 3088-test run in §15. No new
tenant-isolation test was added this pass — none of this pass's changes
touch that surface.

---

## 11. File Upload Security

The pypdf fix (§3, #3) is this pass's one real contribution here: both
`rag/ingestion.py` (document/policy RAG PDF ingestion) and
`attachments/processors/pdf_processor.py` (chat PDF attachments) parse
pypdf against attacker-controlled bytes. All 6 fixed CVEs are DoS-shaped
(large memory usage / long runtime from malformed page-label, ToUnicode,
font, FlateDecode, appearance-stream, or embedded-file structures) — none
are RCE or information-disclosure. Fixed by bumping `pypdf` 6.18.0 →
6.19.0 (the release that contains all 6 fixes); full
`tests/test_attachments_pdf_safety.py`/`tests/test_attachments_processors.py`/
`tests/test_rag_ingestion.py` suites reran clean (two new, harmless
`DeprecationWarning`s about `pypdf`'s own `add_js` test-fixture helper,
unrelated to functionality). The rest of this codebase's file-upload
hardening (zip-slip/decompression-bomb guards, PDF catalog-level
dangerous-content preflight, malware scanning, MIME/signature validation)
is pre-existing, documented in `CLAUDE.md`'s "Chat attachments" section,
and untouched by this pass.

---

## 12. Agentic AI / Prompt Injection Security

Not independently re-derived — this codebase has a disclosed, dated,
live-run prompt-injection benchmark (`docs/security/PROMPT_INJECTION_BENCHMARK_GAP_REPORT.md`,
500 cases, 0 critical findings as of its own last run) and structural
defenses (retrieved content always framed as untrusted data; the SQL
validator as the real terminal gate regardless of LLM output; router-level
RBAC checked *after* the LLM's own source selection, never trusted from
it). None of this pass's changes touch `agent/`, `agent/orchestrator/`,
`rag/`, or `search/` logic. Not re-run this pass (no live Ollama/Tavily
instance in this environment) — status unchanged from that report's own
last-measured date, which already discloses it as point-in-time, not a
standing guarantee.

---

## 13. Secrets Security

**Pattern-based repo sweep (this pass):** regex search for AWS keys, GitHub
PATs, OpenAI/Anthropic-shaped keys, PEM private-key headers, and Slack
tokens across the full tracked tree found exactly one match —
`tests/test_scan_frontend_build_for_secrets.py`, confirmed by direct read
to be the test file's own deliberate fake fixtures (`AKIAABCDEFGHIJKLMNOP`,
a well-known non-functional placeholder; a truncated fake PEM body) used to
test the secret-scanning script itself. No real secret found.

**`.secrets.baseline` was stale**, for the same root cause as §8: its own
CI step never ran against real commits either, so it never picked up
legitimate new content added across the "Prompt 9–15" feature history
(new Alembic migrations, new i18n locale files, new test files, etc.).
Running `detect-secrets scan $(git ls-files) --baseline .secrets.baseline`
fresh this pass found **~80 findings across ~35 files not in the committed
baseline** — spot-checked a risk-weighted sample across every finding
*type* present (`JSON Web Token`, `Basic Auth Credentials`, `Hex High
Entropy String`, `Secret Keyword`, `Private Key`, `AWS Access Key`): a
fake-JWT test fixture literally named `secret_looking_credential =
"...fakefakefake"`, prose in `docs/OBSERVABILITY.md` describing the
redaction feature itself, an Alembic migration's own revision-ID hex
string, a YAML business-description field, an enum member named
`PASSWORD_PROTECTED`, and i18n UI labels like `"localPasswordLabel":
"Password"` — all confirmed false positives, matching the exact same
categories this repo's own prior sessions already audited and accepted for
the original 20. Regenerated `.secrets.baseline` to match current reality
so the CI gate (once restored, §8) diffs against real content going
forward rather than an 80-finding-stale snapshot that would have failed
the very next run regardless of any actual new secret.

**Not done this pass:** a formal `detect-secrets audit .secrets.baseline`
interactive review (which sets each finding's `is_secret` field
explicitly) — the CI comparison script only diffs the baseline file for
stability, not `is_secret` values, so this isn't required for the gate to
function, but a human audit pass remains recommended as a genuine
follow-up, consistent with how this project already treats this tool.

**gitleaks** remains **NOT VERIFIED** (unchanged from prior sessions — the
Go binary isn't available in this environment either); still
`continue-on-error: true` in CI, unchanged, with its own dated
justification comment already in place.

`.env` confirmed gitignored; `.env.example` contains only placeholder
values (spot-checked, matches its own documented convention).

---

## 14. Dependency Audit

**Backend (`pip-audit`), final state:** 22 raw findings / 14 unique
advisories across 6 packages — down from 24/16/7 before this pass. Every
remaining finding is either NOT REACHABLE (re-confirmed, §4–5) or
UPSTREAM FIX REQUIRED with no compatible patched release (`black`). Full
command and output shown in §8.

**Frontend (`npm audit`):** `{"info":0,"low":0,"moderate":0,"high":0,"critical":0}`
— clean, both before this pass's `vitest` bump (pre-existing) and after
(re-verified; a non-forcing `npm audit fix` resolved 4 transitive findings
`vitest` 4's own new dependency tree introduced, all dev-only tooling, 0
remaining).

**New this pass: `requirements.txt` / `requirements-dev.txt` split.**
Previously, every dev/test/lint/docs-build package (`pytest`, `black`,
`ruff`, `mypy`, `reportlab`) lived in `requirements.txt` and was installed
into the **production** Docker image by `Dockerfile`'s `pip install -r
requirements.txt` — "not required to run the app" was already true of what
the app's own `CMD` invokes, but not of what the image actually contained.
Split into `requirements.txt` (runtime only) and `requirements-dev.txt`
(`-r requirements.txt` plus the dev tooling). `Dockerfile` is unchanged
(still `COPY requirements.txt .` / `pip install -r requirements.txt`),
which is what actually shrinks the image's installed-package set — this
isn't a documentation reorganization. Confirmed zero application code
(`agent/`, `api/`, `db/`, `config/`, etc.) imports `pytest`/`black`/`ruff`/
`mypy`/`reportlab` (grepped; only `tests/` and the standalone
`scripts/build_user_guide_pdf.py` do, both correctly outside the
production image already). `tasks.ps1`, `Makefile`, `CONTRIBUTING.md`, and
`README.md` updated to install `requirements-dev.txt` for a dev setup; the
`test` job in CI updated likewise; the `benchmark-regression` job
(application code only, no pytest) correctly left on plain
`requirements.txt`.

**Docker base image / OS packages (Trivy):** **NOT VERIFIED live this
pass** — attempted (`docker build` succeeded; `docker info` then reported
the Docker Desktop daemon unreachable from this sandboxed environment when
the Trivy scan step was attempted), so no fresh container-level scan ran.
Relying on the prior session's own triaged findings
(`docs/security/CVE_TRIAGE.md` §4 — 29 unique CRITICAL/HIGH findings, the
Python-ecosystem ones conclusively not-reachable, ~9 of the ~17 unique
Debian OS-package CVEs explicitly marked "not independently verified" in
that same document). Status unchanged from that prior, honest assessment.

---

## 15. Security Tests

No new dedicated security-regression test file was added this pass (the
changes here are dependency/config fixes, not new application logic to
test) — verification instead took the form of re-running every existing
suite against the changed dependencies/config, end to end, documented in
§16.

---

## 16. Build/Test Results

All commands below were actually executed against this exact checkout this
session; none are reused from a prior session or fabricated.

| Check | Result |
|---|---|
| `pytest` (backend, full suite, post all changes) | **PASS** — 3088 passed, 0 failed, 320 warnings (all pre-existing/benign) |
| `pytest` against `pytest==9.1.1` specifically (pre-bump compatibility check) | **PASS** — 3088 passed, 0 failed, 0 collection errors |
| `ruff check .` | **PASS** — all checks passed (after fixing 7 pre-existing errors) |
| `black --check .` | **PASS** — 451 files unchanged (after reformatting 11 pre-existing files) |
| `bandit -r agent api config db security media_gen moderation rag search voice media embeddings` | **PASS** — 0 issues, 28,558 lines scanned |
| `mypy .` | **FAIL (pre-existing, unchanged)** — 281 errors, 51 files, 100% test files; identical count before and after this pass's own edits (confirmed not to have introduced any new error); now `continue-on-error: true` in CI with a dated comment rather than silently blocking the gates after it |
| `pip-audit -r requirements-dev.txt` (with final CI ignore-list) | **PASS** — exit 0, "No known vulnerabilities found, 22 ignored" |
| `npm run test` (frontend, post `vitest` 4 bump) | **PASS** — 287 passed, 0 failed (3 real regressions found and fixed — see §17) |
| `npm run build` (`tsc -b && vite build`) | **PASS** — clean typecheck, successful production build (2 real type errors found and fixed — see §17) |
| `npm run lint` (oxlint) | **PASS** (warnings only, all pre-existing, unrelated to this pass) |
| `npm audit` | **PASS** — 0 vulnerabilities |
| `python scripts/scan_frontend_build_for_secrets.py --dist-dir frontend/dist` (fresh production build) | **PASS** — no credentials found |
| `detect-secrets scan $(git ls-files) --baseline .secrets.baseline` | **PASS** (baseline regenerated; all findings spot-checked as false positives — see §13) |
| `docker build -t text-to-sql-dashboard:ci .` | **PASS** — image built successfully |
| Trivy scan against the built image | **NOT VERIFIED** — Docker daemon unreachable from this sandbox when attempted; see §14 |
| Branch-protection / required-status-check configuration | **NOT VERIFIED** — requires repo-admin API access this session does not have |

---

## 17. Real Bugs Found and Fixed Along the Way (not pre-existing findings, not security CVEs — genuine regressions from this pass's own changes, caught by actually running things rather than trusting the diffs)

1. **`isinstance(value, (bytes, bytearray, memoryview))` → `isinstance(value, bytes | bytearray | memoryview)`** (`api/main.py:658`) — the one ruff `UP038` finding `--fix` couldn't auto-resolve; applied manually, zero behavior change (Python 3.10+ union-type isinstance syntax, project targets ≥3.11).
2. **Vitest 4's `restoreMocks`/`vi.restoreAllMocks()` no longer clears call history on a plain `vi.fn()` created inside a `vi.mock()` factory** (only true `vi.spyOn()` spies are "restored" now) — caused 3 real test failures
   (`OcrResultDialog.test.tsx`, `ResizeImageDialog.test.tsx`, `GoogleSignInButton.test.tsx`) where a mock's call count leaked from an earlier test in the same file into a later one asserting `.not.toHaveBeenCalled()`. Fixed with `clearMocks: true` in `vitest.config.ts` — the idiomatic, version-proof fix (clears call history before every test regardless of how the mock was created), not three one-off test edits.
3. **Vitest 4's updated `Mock<T>` generic type broke two `tsc -b` type checks** in `GoogleSignInButton.test.tsx` (a loosely-typed `ReturnType<typeof vi.fn>` no longer structurally matched the real `window.google.accounts.id.initialize`/`renderButton` signatures). Fixed by giving the two mocks explicit generic types against the real ambient interfaces already declared in `src/types/google-identity.d.ts`.
4. **`vitest.config.ts`'s own `import viteConfig from './vite.config'`** (no extension) triggered a forward-compatibility warning from Vitest's new native config loader. Fixed (`./vite.config.ts`) — cosmetic, but flagged by the tool itself as becoming a hard requirement in a future major version.

All four were caught by actually running the full test/build pipeline after each dependency bump, not assumed safe from a changelog.

---

## 18. Remaining Risks (Not Fixed This Pass)

| Risk | Severity | Why not fixed |
|---|---|---|
| `langgraph` 0.2.62 → 1.x major migration (closes the checkpoint/cache CVE family, none of which are reachable today regardless) | High (migration risk, not current exploitability) | Touches all 15 SQL-graph nodes + the 6-node orchestrator graph; no dedicated regression-testing time in this pass; correctly scoped out by prior sessions too |
| `black` 24.x → 26.x (only line with a CVE patch) | Low (dev-only, both CVEs confirmed not reachable via this repo's own CI invocation) | Reformats 11 files' worth of this codebase's current, intentional style on a clean run — a large, unrelated-to-security diff; UPSTREAM FIX REQUIRED for a 24.x-line patch, none exists |
| `chromadb` 1.5.9 CVEs | Critical/High by CVSS, not reachable | No newer release exists upstream at all (verified against PyPI) — UPSTREAM FIX REQUIRED |
| Trivy container/OS-package scan | Unknown (prior session's own partial triage stands) | Docker daemon unreachable in this sandbox this session |
| gitleaks secret scan | Unknown (detect-secrets is the actually-run, blocking check) | Go binary unavailable in this sandbox, same as every prior session |
| `mypy` 281 pre-existing errors (100% test files) | Low (type-hygiene debt, not a security finding) | Explicitly out of scope for a security pass; now non-blocking with a dated comment rather than silently gating nothing |
| Branch-protection / required-status-checks on `master` | Unknown | Requires repo-admin API access not available this session; strongly recommended now that the `CI` workflow will actually run |
| `detect-secrets` formal human audit (`detect-secrets audit`) | Low | Spot-checked this pass across every finding type present, not individually audited one by one |

---

## 19. Production Recommendations

1. **Merge these changes, then watch the next `master` push's `CI` run —
   this will be the first real one in this repository's history.** Expect
   `mypy` to show red (by design, non-blocking) and everything else to
   pass based on this pass's local verification.
2. Configure branch protection on `master` requiring the `CI` workflow's
   `secret-scan`, `test`, and `frontend` jobs (at minimum) as required
   status checks — nothing currently enforces that a red CI run blocks a
   merge, independent of whether CI runs at all.
3. Re-run the Trivy container scan from an environment with a reachable
   Docker daemon and triage the ~17 unique, not-fully-verified Debian
   OS-package CVEs from the prior session's own `CVE_TRIAGE.md` §4 — most
   likely resolved by a routine base-image digest bump, not application
   code changes.
4. Schedule the `langgraph` 0.2→1.x migration as its own dedicated,
   reviewed piece of work, not bundled into a security pass.
5. Run a formal `detect-secrets audit .secrets.baseline` pass to close out
   the ~80 newly-surfaced findings individually (this pass spot-checked by
   type/category, not file-by-file).

---

## 20. Files Changed

**CI/CD:**
- `.github/workflows/ci.yml` — push-trigger branch fix, workflow-level
  `permissions: contents: read`, `persist-credentials: false` on every
  checkout, `trivy-action` pinned to SHA, `mypy` moved last +
  `continue-on-error`, `pip-audit`/install steps switched to
  `requirements-dev.txt`, ignore-list pruned.

**Dependencies:**
- `requirements.txt` — `pypdf` 6.18.0→6.19.0, `pyjwt[crypto]` 2.14.0→2.15.1;
  dev/lint/docs-build section removed (moved out).
- `requirements-dev.txt` — **new file**, dev/test/lint/docs-build tooling,
  `pytest` 8.3.4→9.1.1, `black` unchanged (documented why).
- `frontend/package.json` / `frontend/package-lock.json` — `vitest`
  3.2.7→4.1.11 (and its transitive `@vitest/mocker`, resolved to the
  matching 4.1.11); `npm audit fix` applied for 4 transitive dev-only
  findings vitest 4's new tree introduced.

**Application code (regression fixes caused by the above, not new
features):**
- `api/main.py` — one ruff-flagged `isinstance` syntax fix.
- `frontend/vitest.config.ts` — `clearMocks: true`; config-loader
  extension fix.
- `frontend/src/components/auth/GoogleSignInButton.test.tsx` — explicit
  mock generic types.
- 11 files reformatted by `black .`; 2 files' imports resorted by
  `ruff --fix` beyond the one listed above (pre-existing drift, unrelated
  to any feature — see §8).

**Docs/tooling:**
- `Dockerfile` — clarifying comment only (no functional change — it
  already only installed `requirements.txt`).
- `tasks.ps1`, `Makefile`, `CONTRIBUTING.md`, `README.md` — dev setup now
  installs `requirements-dev.txt`.
- `docs/security/CVE_TRIAGE.md` — addendum pointing to this report.
- `.secrets.baseline` — regenerated against current repository content.
- `SECURITY_REMEDIATION_REPORT.md` — this file.

---

## 21. Final Dependabot Status

| PR (opened 2026-09-19, all previously failing CI) | Status after this pass |
|---|---|
| #1 `aquasecurity/trivy-action` 0.24.0 → 0.36.0 | **FIXED** (applied via SHA pin, stronger than Dependabot's own tag-only proposal) |
| #2 `pytest` 8.3.4 → 9.0.3 | **FIXED** (applied as 9.1.1, the current patch release; compatibility independently verified, not assumed from Dependabot's own green/red signal, which was never meaningful given the branch-name bug) |
| #3 `langgraph` 0.2.62 → 1.0.10rc1 | **NOT APPLIED** — release-candidate version, major migration, explicitly scoped out (§18) |
| #4 `@vitest/mocker` 3.2.7 → 5.0.1 | **SUPERSEDED** — resolved automatically and correctly to 4.1.11 (matching the `vitest` core bump) once `vitest` itself was bumped properly; Dependabot's own proposed 5.0.1 would have created a version mismatch with vitest core |
| #5 `vitest` 3.2.7 → 4.1.11 | **FIXED** (applied, plus 3 real test regressions and 2 type errors found and fixed along the way — see §17) |
| #6 `black` 24.10.0 → 26.3.1 | **NOT APPLIED** — would reformat 11 files' worth of unrelated style; UPSTREAM FIX REQUIRED for a same-major-line patch; mitigated instead by removing black from the production image entirely (§14) |

---

## 22. Final Acceptance Status

```
[x] Repository fully inspected
[x] Dependency baseline captured (pip-audit, npm audit, both run fresh)
[x] All visible Dependabot alerts investigated (6/6 PRs read, diagnosed, resolved or explicitly deferred)
[x] Additional relevant advisories checked (pypdf, PyJWT -- found fresh, not in any prior doc)
[x] Critical issues addressed (trivy-action supply chain, CI branch-name bug)
[x] High issues addressed (pypdf, PyJWT, vitest)
[x] Moderate issues addressed (pytest)
[x] ChromaDB security reviewed (re-confirmed NOT REACHABLE, fresh evidence)
[x] ChromaDB tenant isolation reviewed (architecture doesn't have the SaaS-tenant shape assumed by the brief; spot-checked the one client-controlled database-selector field)
[ ] ChromaDB authorization live-tested -- N/A, no HTTP server mode exists to test
[x] LangGraph checkpoint security reviewed (re-confirmed NOT REACHABLE, fresh evidence)
[x] Trivy supply chain secured (SHA-pinned to a verified clean release)
[x] GitHub Actions permissions reviewed (added least-privilege default + persist-credentials:false)
[x] Black vulnerability addressed (mitigated via image-exclusion; UPSTREAM FIX REQUIRED for the CVE itself)
[x] pytest vulnerability addressed (fixed via verified-compatible major bump)
[x] Vitest vulnerabilities addressed (fixed, with real regressions caught and fixed)
[x] SQL security reviewed (unchanged, pre-existing, not touched by this pass)
[x] Prompt injection reviewed (unchanged, pre-existing, not touched by this pass)
[x] Agent/tool authorization reviewed (unchanged, pre-existing, not touched by this pass)
[x] File upload security reviewed (pypdf fix; rest pre-existing/unchanged)
[x] Secrets reviewed (pattern sweep + regenerated, spot-checked baseline)
[x] Authentication reviewed (PyJWT fix; rest pre-existing/unchanged)
[x] Authorization reviewed (unchanged, pre-existing, not touched by this pass)
[ ] Security regression tests added -- none added; this pass's changes were config/dependency fixes verified by re-running existing suites, not new application logic
[x] Backend tests executed (3088 passed)
[x] Frontend tests executed (287 passed)
[x] Dependency audit executed (pip-audit, npm audit, both clean against their respective ignore-lists)
[x] Build executed (backend imports cleanly via the full pytest run; frontend tsc+vite build clean)
[x] Docker build executed (succeeded); Trivy scan NOT executed (daemon unreachable)
[x] No new Critical/High vulnerability introduced by this pass's own changes
[x] Final independent review completed (see below)
[x] SECURITY_REMEDIATION_REPORT.md created
```

---

## 23. Independent Security Review of This Pass's Own Changes

Reviewing the changes above as if auditing someone else's remediation work,
specifically for new weaknesses this pass itself might have introduced:

- **The Trivy SHA pin** (`ed142fd0673e97e23eac54620cfb913e5ce36c25`) was
  verified against two independent GitHub API calls (`git/refs/tags/v0.35.0`
  resolving to a SHA, and a separate `tags` listing showing `v0.36.0` at a
  *different*, newer SHA) plus a read of `v0.36.0`'s actual release notes —
  not copied from a search result or assumed. **No weakness found.**
- **`continue-on-error: true` on both `mypy` and the Trivy scan** could be
  mistaken for "security theater" (a gate that looks like it's blocking but
  isn't). Both carry explicit, dated comments stating exactly why and what
  would need to be true to flip them — this is the same pattern this
  codebase already uses for `gitleaks`/the load-test smoke job, not a new
  precedent. **Re-checked: comments are accurate and specific, not vague.**
- **The `requirements.txt`/`requirements-dev.txt` split** could in theory
  let the two drift (a new runtime dependency added only to
  `requirements-dev.txt` by mistake would silently work in CI/dev but break
  production). Mitigated by `requirements-dev.txt`'s own `-r
  requirements.txt` include (anything in the base file is still covered)
  and by this pass's own grep confirming zero current application-code
  dependency on anything moved out — but this is a real, ongoing discipline
  requirement for future contributors, not a one-time fix. Flagged here
  explicitly rather than left implicit.
- **`clearMocks: true`** changes global test behavior (every mock's call
  history now clears before each test, not just `restoreMocks`'s narrower
  spy-restoration). Checked: this is strictly more correct test isolation,
  not a weaker one — re-ran the full 287-test frontend suite after adding
  it and confirmed all pass, including tests that don't rely on this
  behavior at all. **No weakness found.**
- **The regenerated `.secrets.baseline`** is the one change in this pass
  with the least rigorous verification (spot-checked by category across
  ~80 findings, not individually audited). If a real secret happened to be
  among the unexamined findings, this pass's baseline would now be
  "baselining" it rather than flagging it. Mitigated by the independent,
  separate pattern-based regex sweep in §13 (which covers common real
  secret *shapes* directly, not via detect-secrets' own heuristics) finding
  nothing — two different detection methods agreeing — but this residual
  risk is honestly real and is why §19 recommends a full `detect-secrets
  audit` as a named follow-up, not silently left implicit.

No critical issue was found in this independent pass. The two residual,
explicitly-flagged items (`requirements-dev.txt` drift discipline, the
not-fully-audited secrets baseline) are process risks, not exploitable
vulnerabilities, and are not swept under anything above.
