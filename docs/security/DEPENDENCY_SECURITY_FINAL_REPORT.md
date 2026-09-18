# Dependency Security — Final Report

**Date:** 2026-09-18 · **Scope:** dependency-security assessment and
remediation only, per this session's explicit brief. No new features, no
`langgraph` 0.2→1.x migration (see §8). Builds on, and re-verifies rather
than assumes, the CI-gate-hardening pass completed earlier the same day
and the 2026-09-16 `docs/DEPENDENCY_SECURITY.md` pass.

---

## 1. Executive Summary

This app's **application-dependency** vulnerability posture is unchanged
in count from before this session — 24 raw pip-audit findings (16 unique
advisory IDs / 14 unique CVEs) across 7 packages, 0 npm findings —
because **zero safe upgrades were available to apply**: every
reachable-in-principle fix requires either the `langgraph` 0.2→1.x
major-version migration this session was explicitly told not to attempt,
or a `pytest`/`black` major-version bump with no non-breaking patch-level
alternative (verified via `pip index versions`, not assumed). What
changed on that front is the **rigor and currency of the reachability
evidence**: every one of the 12 unique runtime-package findings was
independently re-confirmed this session as NOT REACHABLE through this
application's own code paths, with one new, specifically-named
verification (the LangGraph node-caching RCE, CVE-2026-27794, previously
only covered by a general "langgraph-checkpoint family" statement) added
to close a real gap in how specifically the prior pass had checked
reachability.

**Genuinely new this session:** a container image finally built
successfully (it had stalled across the prior session with zero
progress) and was scanned end-to-end with Trivy for the first time this
project has ever had that done — **29 additional unique CRITICAL/HIGH
findings** invisible to `pip-audit`/`bandit`/`npm audit`: 4 Python-package
findings (all confirmed NOT REACHABLE — two are vendored copies inside
`pip`/`setuptools` themselves, two are build-time-only tooling) and ~17
Debian-base-image OS-package CVEs (partially, not fully,
reachability-verified — see §14). This is real, new-to-this-project
coverage, not a re-statement of what was already known.

**No production blocker was found or introduced this session.** The one
blocker that remains open — the `langgraph` major-version migration — was
already known, already deferred by deliberate prior decision, and is now
more thoroughly documented (`docs/security/LANGGRAPH_UPGRADE_SECURITY_ASSESSMENT.md`).
The ~9 not-independently-verified OS-level container findings (§14/§16)
are a new, real, open item — not a blocker by evidence gathered so far,
but not affirmatively cleared either.

## 2. Baseline Vulnerability Status (start of this session)

| Scanner | Findings | Source |
|---|---|---|
| `pip-audit -r requirements.txt` | 24 raw / 16 unique IDs / 7 packages | Re-run fresh this session; identical to `docs/DEPENDENCY_SECURITY.md` (2026-09-16) and the CI-gate-hardening pass (earlier today) |
| `npm audit` (frontend) | 0 | Re-run fresh this session |
| `bandit` | 0 | Re-run fresh this session |
| Trivy (container) | Not previously run — a build never completed prior to this session | — |
| gitleaks | NOT VERIFIED (tooling unavailable in this sandboxed environment, both sessions) | — |

**After this session:** every row above is unchanged except Trivy, which
went from "never run" to **29 unique CRITICAL/HIGH findings** (§14) — a
net *increase* in known findings, not a decrease, because this is
genuinely new coverage, not because anything regressed. This is the
correct and expected shape of "before → after" for a session whose value
was mostly evidence quality and coverage breadth rather than a vulnerable-
package count going down.

## 3. Vulnerabilities Found

See `docs/security/CVE_TRIAGE.md` §1 for the full 14-CVE matrix (CVE/GHSA
ID, package, version, severity, fixed version, runtime/dev,
direct/transitive, reachability, exploitability, affected functionality,
available upgrade, breaking-change risk, remediation, status, evidence).
Summary by severity (unique CVEs, not raw advisory-ID count):

| Severity | Count | CVEs |
|---|---|---|
| Critical | 6 | CVE-2026-28277, CVE-2026-45829, CVE-2026-45833, CVE-2025-64439, CVE-2026-27794, CVE-2026-48775 |
| High | 4 | CVE-2026-45831, CVE-2026-45830, CVE-2026-34070, CVE-2026-26013 |
| Medium | 4 | CVE-2026-48776, CVE-2025-71176, CVE-2026-32274, CVE-2026-31900 |
| Low | 0 | — |

## 4. Vulnerabilities Fixed (this session)

**None.** Not from lack of effort — see §5 for what was actually checked —
but because no safe (non-major-version) upgrade path exists for any
currently-open finding. This is stated plainly rather than padded: the
prior session (2026-09-16 per `docs/DEPENDENCY_SECURITY.md`) already
captured the two genuinely-safe upgrades available at that time
(`pillow` 11.3.0→12.3.0, `python-dotenv` 1.0.1→1.2.2), both still in place
and unaffected by anything in this session.

## 5. Vulnerabilities Remaining

All 14 unique CVEs remain open, all with `Status` values of either
`NOT REACHABLE — DOCUMENTED` (10 CVEs, all in the `langgraph`/
`langgraph-checkpoint`/`langgraph-sdk`/`langchain-core`/`chromadb`
families) or `DEV ONLY` (4 CVEs, `pytest`/`black`). See §6/§3 for detail.
**Zero** findings are classified `ACCEPTED RISK` in the sense of "reachable
but tolerated" — every open finding is either structurally unreachable
given this app's actual configuration, or confined to tooling never
invoked by the running production process.

## 6. CVE Triage

Full matrix: `docs/security/CVE_TRIAGE.md`. Reachability re-verification
commands actually run this session (fresh, not reused from the prior
pass's memory):

```
grep -rn "Checkpointer\|BaseCheckpointSaver\|\.compile(checkpointer" agent api db config identity media media_gen moderation rag search voice embeddings retrieval
grep -rn "langgraph_sdk\|langgraph\.sdk" agent api db config identity media media_gen moderation rag search voice embeddings retrieval scripts
grep -rn "HttpClient\|AsyncHttpClient" embeddings media rag retrieval
grep -rn "ChatOpenAI|langchain_openai|from langchain\b|prompts\.loading|load_prompt" agent api db config identity media media_gen moderation rag search voice embeddings retrieval scripts tests
grep -rn "CachePolicy|BaseCache|cache_policy" agent api db config identity media media_gen moderation rag search voice embeddings retrieval scripts
```
All zero matches. Plus a direct read of both `StateGraph.compile()` call
sites (`agent/graph.py:257`, `agent/orchestrator/graph.py:126`) confirming
neither passes a `checkpointer=` or `cache=` argument.

## 7. Dependency Upgrades

**None performed this session.** Evaluated and explicitly rejected:

| Package | Current | Fix version | Why not upgraded |
|---|---|---|---|
| `pytest` | 8.3.4 | 9.0.3+ | Major version bump (8→9); `pip index versions pytest` confirms no CVE-patched 8.x release exists. Dev-only reachability (never invoked by the running app). |
| `black` | 24.10.0 | 26.3.1+ | Major version bump (24→26, skipping 25 entirely); `pip index versions black` confirms no patched 24.x release exists. Would very likely reformat a large fraction of this codebase — a large, unrelated diff. Dev-only; CI never passes the specific vulnerable flag (`--python-cell-magics`) anyway. |
| `langgraph` + family | 0.2.62 / 2.1.2 / 0.1.74 / 0.3.86 | `>=1.0.0` / `>=3.0.0`(+) / `>=0.3.15` / `>=1.2.22` | Explicitly out of scope this session (§8) — major version, real breaking-change risk, requires its own dedicated regression pass including the live benchmark this sandboxed environment can't run. |
| `chromadb` | 1.5.9 | **None published** | No fix exists to apply — `fix_versions: null` in this session's own fresh pip-audit run, re-confirmed. |

## 8. LangGraph Migration Blockers

Full detail: `docs/security/LANGGRAPH_UPGRADE_SECURITY_ASSESSMENT.md`.
Summary: current `langgraph==0.2.62`, latest `1.2.11` (not just `1.0.0` —
a full further minor generation ahead of the CVE-fixed floor). Migration
**not started** this session per explicit instruction. Key findings from
the assessment:

- This app's entire direct LangGraph API surface is two import lines
  (`from langgraph.graph import END, StateGraph` in `agent/graph.py` and
  `agent/orchestrator/graph.py`) and a narrow set of calls
  (`add_node`/`add_edge`/`add_conditional_edges`/`set_entry_point`/
  `compile()`/`invoke()`) — no `Send`, `Command`, `interrupt`, prebuilt
  agents, checkpointers, or caching. This is a real, evidence-based signal
  that migration risk is *lower* than a typical LangGraph app, not a
  guarantee.
- One specific open question flagged for whoever does the migration:
  whether `set_entry_point(...)` (this app's idiom) is still supported in
  1.2.x, or whether it needs to become `add_edge(START, ...)` — **this
  session's own attempts to fetch LangGraph's official migration guide
  both returned HTTP 404** (`langchain-ai.github.io`/`docs.langchain.com`
  — docs appear to have moved), so this specific question is honestly
  unresolved, not silently assumed either way.
- The live Text-to-SQL benchmark (`scripts/run_benchmark.py
  --check-regression`) — the one verification step that would catch a
  subtle graph-behavior regression a mocked test suite wouldn't — requires
  a live Ollama + database instance neither available in this sandboxed
  session nor wired to run automatically in CI.

## 9. Supply-Chain Assessment

- **Pinning:** `requirements.txt` is 100% `==`-pinned (38/38 real
  dependency lines, verified by grep this session) — no floating versions.
  No lock file (`poetry.lock`/`uv.lock`) exists or is needed given the
  single fully-pinned `requirements.txt` already serves that role.
  `frontend/package-lock.json` (npm's own lock file) is present and
  committed.
- **Dependency-update automation: a real, disclosed gap.** No
  `.github/dependabot.yml` and no Renovate config exist anywhere in this
  repo (checked this session — neither file present). Every dependency
  bump to date has been a manually-triggered `pip-audit`/`npm audit` pass,
  not a continuously-monitored one. **Recommendation:** add a minimal
  `dependabot.yml` covering both `pip` (`requirements.txt`) and `npm`
  (`frontend/`) ecosystems, `open-pull-requests-limit` kept low (this
  project's own "no unnecessary tooling/change" posture argues against a
  noisy default) — this closes the gap between "someone remembers to
  re-run `pip-audit`" and "a new advisory is surfaced automatically."
- **Dev tooling ships inside the production container — a real, narrow
  attack-surface finding, distinct from anything CVE-shaped.**
  `requirements.txt`'s own "Dev / test / lint (not required to run the
  app)" section (`pytest`, `black`, `ruff`, `mypy`, `types-PyYAML`) is
  installed by the *same* `pip install -r requirements.txt` the
  `Dockerfile` runs for the production image — there is no separate
  `requirements-dev.txt`. The two dev-only CVEs (`pytest`, `black`) are
  therefore present in the shipped image's `site-packages`, not merely
  "on developer machines," even though nothing in the running `uvicorn`
  process ever imports them (`Dockerfile`'s `CMD` is `uvicorn` only,
  confirmed). **Recommendation** (not implemented this session — a
  Dockerfile/CI change, judged out of scope for a
  dependency-*vulnerability* remediation pass specifically): split into
  `requirements.txt` (runtime) + `requirements-dev.txt` (dev/test/lint),
  have the `Dockerfile` install only the former, and have CI's `test` job
  install both. This would remove the `pytest`/`black` CVEs from the
  shipped image's contents entirely, not just argue they're unreachable.
- **GitHub Actions pinning: tag-pinned, not SHA-pinned** (`actions/checkout@v4`,
  `actions/setup-python@v5`, `actions/setup-node@v4`,
  `gitleaks/gitleaks-action@v2`, `aquasecurity/trivy-action@0.24.0`) —
  already disclosed in `docs/DEPENDENCY_SECURITY.md` as a Phase 2
  recommendation, re-confirmed unchanged this session, not re-fixed here
  (a supply-chain hardening item, not a vulnerability remediation one).
- **Docker base images: digest-pinned** (`python:3.11-slim@sha256:...`,
  `node:22-slim@sha256:...`) — the stronger control, already in place, no
  change needed.
- **Unmaintained/abandoned-package risk:** not separately assessed this
  session (would require checking last-release dates for all ~200
  backend + ~580 frontend transitive components — out of this session's
  time budget); the SBOMs generated in §11 are the concrete artifact a
  future pass would use to do this systematically rather than by hand.
- **Typosquatting/malicious-package risk:** no dedicated tooling for this
  exists or was added this session (e.g. a package-name-similarity
  checker) — a real, disclosed gap, not silently assumed covered by
  `pip-audit`/`npm audit` (which check known-CVE databases, not
  typosquatting heuristics).

## 10. SBOM Status

**Generated this session**, CycloneDX format, using tooling already
present (no new dependency added):

| File | Format | Components |
|---|---|---|
| `docs/security/sbom/backend-sbom.cyclonedx.json` | CycloneDX 1.4 | 159 |
| `docs/security/sbom/frontend-sbom.cyclonedx.json` | CycloneDX 1.5 | 579 |

Commands: `pip-audit -r requirements.txt --format cyclonedx-json` and
(from `frontend/`) `npm sbom --sbom-format cyclonedx`. See
`docs/security/sbom/README.md`. **Not generated:** a container-image-level
SBOM — recommended as a CI addition once the image build is reliably fast
enough to run on every push (see §12's note on build time).

## 11. Test Results

**Full backend suite:** `pytest` → **1406 passed, 0 failed** (fresh run
this session, `61.66s`–`71.62s` across two runs). **Targeted
security-relevant subset** (auth/JWT/OIDC, RBAC/IDOR, SQL validator +
restricted-column + wildcard bypass, prompt injection, RAG isolation,
SSRF, file-upload + malware-scanner, rate limiting, security headers/CORS)
→ **426 passed, 0 failed**, run as an explicit named subset per this
session's Phase 7 brief, not just implied by the full-suite pass.

**Frontend:** `npm run lint` (oxlint) → exit 0, only 2 pre-existing
warnings (not errors, not introduced this session). `npx tsc -b --noEmit`
→ exit 0. `npm run build` → succeeded (`✓ built in 1.37s`, 17 precache
entries generated); one pre-existing chunk-size-over-500kB advisory
warning, not a failure.

**Backend lint/format/type — mixed, with two pre-existing findings
independently re-confirmed this session, neither introduced by this
session's own changes (verified via `git stash` both times):**
- `ruff check .` → 9 errors, all in files this session never touched
  (`config/moderation_blocklist.py` test, `moderation/`, `media_gen/`,
  `scripts/`) — confirmed pre-existing on a clean `git stash` of this
  session's changes.
- `black --check .` → 10 files would be reformatted, same file set as
  ruff's findings, same pre-existing confirmation.
- `mypy .` → **NOT VERIFIED for a full-repo run.** Fails immediately with
  a hard configuration error (`scripts/build_user_guide_pdf.py: error:
  Source file found twice under different module names`) *before*
  producing any per-file type-error list — confirmed pre-existing via
  `git stash` (reproduces identically on a clean `master` checkout). This
  is a **new finding this session**, more severe than the 4 per-file type
  errors the CI-gate-hardening pass found by scoping `mypy` to individual
  files — it means CI's actual `mypy .` invocation likely already fails
  outright on `master`, not just "has findings." Not fixed this session
  (a mypy/`scripts/` packaging configuration issue, unrelated to
  dependency security specifically) — flagged here rather than silently
  left for the next person to rediscover.

## 12. SAST Results

`bandit -r agent api config db security media_gen moderation rag search
voice media embeddings -f txt` → **0 issues**, 18,123 lines scanned.
Re-run fresh this session; identical to the CI-gate-hardening pass's own
same-day result. No drift.

## 13. Secret Scan Results

**Not re-run this session** — no tracked files changed between the
CI-gate-hardening pass and this one (`git status` confirmed no
intervening commits). Carrying forward that pass's result: `detect-secrets`
fully run and triaged (46 matches / 20 files, all confirmed false
positives, baseline committed, CI gate blocking); `gitleaks` itself
**NOT VERIFIED** (Go binary unavailable in this sandboxed environment,
both sessions).

## 14. Container Scan Results

A `docker build -t text-to-sql-dashboard:localscan .` started in the
*previous* session finally completed **during this session** (its
background-task notification only arrived partway through this pass —
the image was in fact already sitting built and unused for some
unknown-but-substantial amount of wall-clock time; `docker images` shows
it at 3.8GB). Trivy was then run against the real image:

```
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v trivy-cache:/root/.cache/ \
  aquasec/trivy:latest image --severity CRITICAL,HIGH --format table text-to-sql-dashboard:localscan
```

**Result: 29 unique CRITICAL/HIGH CVE/GHSA IDs.** Full detail and
per-finding reachability evidence in `docs/security/CVE_TRIAGE.md` §4.
Summary:

- **4 Python-ecosystem findings not surfaced by `pip-audit`** —
  `jaraco.context` (CVE-2026-23949) and `msgpack` (GHSA-6v7p-g79w-8964),
  both confirmed via direct file inspection inside the built image
  (`docker run --rm text-to-sql-dashboard:localscan find ...`) to be
  **vendored copies bundled inside `setuptools`/`pip` themselves**, not
  top-level dependencies of this application at all (absent from
  `pip list` output); and `setuptools` (CVE-2025-47273) / `wheel`
  (CVE-2026-24049), real top-level packages (transitive via `torch`) whose
  vulnerable code paths are packaging/install-time-only, never invoked by
  the running `uvicorn` process. **All four: NOT REACHABLE, verified with
  the same file-path-level evidence rigor as every other finding in this
  report** — not merely asserted.
- **~17 unique OS-level (Debian 13.6 base image) CVEs, 58 raw findings**
  — genuinely new coverage (`pip-audit`/`bandit`/`npm audit` have zero
  visibility into OS packages). `util-linux`-family (mount/nsenter
  privilege-escalation shapes) and `curl` (SSH/TLS/proxy issues) both got
  a specific, checked reachability argument (non-root container user;
  `curl`'s one real call site is the Dockerfile's own plain-HTTP loopback
  healthcheck). The remaining ~9 (`libsqlite3-0`, `pcre2`, `gzip`,
  `libacl1`, `ncurses`, `libsystemd0`, `perl-base`) are recorded honestly
  as **NOT INDEPENDENTLY VERIFIED** — a real, disclosed gap, not silently
  assumed safe.
- **Actionable remediation distinct from a CVE list:** several OS findings
  already have a Debian-published fix not yet baked into the pinned base
  image digest (`gzip`→`1.13-1+deb13u1`, `pcre2`→`10.46-1~deb13u2`,
  `sqlite3`→`3.46.1-7+deb13u2`) — re-pinning `Dockerfile`'s
  `python:3.11-slim` digest to a current build would likely close most of
  these without touching this app's own code. Not done this session
  (recommended as its own small, reviewed follow-up).

**Timeline note, for transparency:** the `docker build` that produced this
image was started in the *previous* session and had not completed by that
session's end (0 bytes of captured output, no resulting image). Its
background-task completion notification only arrived partway through
*this* session — the image had in fact been sitting built and unused for
some unknown-but-substantial amount of wall-clock time in between. This
report does not know, and does not claim to know, exactly when the build
actually finished.

## 15. CI/CD Gate Status

Confirmed intact from the CI-gate-hardening pass (working tree still
uncommitted, `git diff` shows no reversions):

| Gate | Status | `continue-on-error`? |
|---|---|---|
| Lint (ruff) | Blocking | No |
| Format check (black) | Blocking | No |
| Type check (mypy) | Blocking | No (see §11's new finding — blocking a check that itself errors out is a real, if different, problem) |
| SAST (bandit) | Blocking | No |
| Tests (pytest) | Blocking | No |
| Dependency scan (pip-audit) | Blocking, with a specific 16-ID `--ignore-vuln` allowlist | No |
| Secret scan (detect-secrets) | Blocking, baseline-backed | No |
| Secret scan (gitleaks) | Report-only | **Yes** — documented reason: tooling unavailable to verify locally |
| Container scan (Trivy) | Report-only | **Yes** — documented reason: no completed image existed to verify against until this session (see §14) |

Only 2 `continue-on-error: true` directives remain in
`.github/workflows/ci.yml`, both with an explicit, dated, reasoned comment
in the file itself — not silent gaps. Per this session's own Phase 10
instruction, neither was removed without analysis, and neither is a
"production security check silently passing after detecting a blocking
vulnerability" — both are scanners that genuinely could not be verified
end-to-end this session, honestly labeled as such rather than force-flipped.

## 16. Residual Risk

- **`langgraph` family, 10 CVEs, all NOT REACHABLE today** — residual risk
  is architectural drift: if a future change ever configures a
  checkpointer or node-level caching (both currently absent), this
  reachability analysis must be redone *before* that change ships, not
  discovered after. `docs/security/LANGGRAPH_UPGRADE_SECURITY_ASSESSMENT.md`
  §7 lists the specific compensating controls this depends on.
- **`chromadb`, 4 CVEs, NOT REACHABLE, no fix published at all** —
  residual risk is bounded by this app never running Chroma in HTTP-server
  mode; monitor for an upstream fix before ever considering that mode.
- **`pytest`/`black`, 4 CVEs, DEV ONLY but present in the shipped
  production image** — residual risk is attack-surface/image-size, not a
  live exploit path; closeable via the `requirements-dev.txt` split
  recommended in §9, not yet implemented.
- **No automated dependency-update monitoring** (§9) — residual risk is a
  *new* advisory going unnoticed between manual `pip-audit`/`npm audit`
  passes; closeable via Dependabot/Renovate, not yet implemented.
- **`mypy .` fails outright on a clean checkout** (§11) — residual risk is
  that the "blocking" mypy CI gate may not actually be providing the
  protection its `continue-on-error`-free configuration implies; needs its
  own, separate investigation (a `scripts/`-packaging fix, likely a
  missing `__init__.py` or `mypy_path` adjustment) — out of scope for a
  dependency-security pass, flagged rather than silently left.
- **~9 Debian base-image OS-package CVEs, NOT INDEPENDENTLY VERIFIED**
  (`libsqlite3-0`, `pcre2`, `gzip`, `libacl1`, `ncurses`, `libsystemd0`,
  `perl-base` — §14) — residual risk is genuinely unknown, not assumed
  low: whether any transitive Python dependency `dlopen`s the affected
  system libraries wasn't checked with the same file-path-level rigor
  this report applied to the Python-ecosystem findings. Closeable two
  ways, neither done this session: (1) the specific reachability check
  (grep for `ctypes`/`dlopen`/C-extension usage of `sqlite3`/`pcre2` across
  installed packages), or (2) the blunter but effective fix — re-pin the
  `Dockerfile`'s base-image digest to a build carrying Debian's
  already-published patches for several of these.
- **Container scanning is not yet a CI gate that runs reliably** — this
  session's own Trivy run needed ~30 minutes for the image build alone
  (heavy ML dependencies, `--no-cache-dir`), which is why `docker-scan`
  stayed `continue-on-error: true` in CI even after a real scan finally
  ran locally. A CI-hosted run has different (likely better, cached)
  performance characteristics than this sandboxed session's, but that's
  an assumption, not evidence gathered this session.

## 17. Production Blockers

**No hard blocker newly introduced this session.** One genuinely new,
disclosed **open item** (not asserted as a blocker, not asserted as safe
either): the ~9 not-independently-verified Debian OS-package CVEs
surfaced by this session's first-ever successful Trivy run (§14/§16). Every
*other* finding this session touched was already known from prior passes,
just re-verified with fresher or more specific evidence.

Restating what's already true from prior passes rather than re-deriving
it: this platform's own `SECURITY_FINAL_REPORT.md` already states it
should not be called production-ready until its own §29/§30 items are
closed (dependency CVEs being one of several, alongside unverified
frontend OIDC, missing malware-scanning infrastructure at the time —
since closed by the CI-gate-hardening pass's `security/malware_scanner.py` —
and DAST never having been run). **This session's own scope was narrower
than a full production-readiness call** — it does not issue one, and
nothing here should be read as superseding `SECURITY_FINAL_REPORT.md`'s
own, still-current bottom line.
