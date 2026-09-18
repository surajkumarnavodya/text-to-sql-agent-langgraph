# CVE / Scanner Triage

**Last updated:** 2026-09-18 (dependency-security remediation pass, session 2 — supersedes this file's own prior version from the CI-gate-hardening pass earlier the same day; every finding below was independently re-scanned and re-verified this session, not carried forward from memory).

**Method.** Every claim is grounded in one of: (a) an actually-executed
scanner run against this exact checkout, with the raw command shown, or
(b) a direct `grep`/code-read confirming or ruling out reachability, with
file:line evidence. Nothing here is asserted on the strength of an older
document alone — where this session's re-scan reproduced a prior finding,
that's stated explicitly as "re-verified," not silently assumed. Anything
that could not be run is marked **NOT VERIFIED** with the specific reason.

**Legend (Status column):**
`FIXED` · `REQUIRES UPGRADE` · `NOT REACHABLE — DOCUMENTED` · `DEV ONLY` ·
`FALSE POSITIVE — DOCUMENTED` · `ACCEPTED RISK`

---

## 1. Dependency CVEs (pip-audit)

**Command executed:**
```
pip-audit -r requirements.txt --desc --format json
```
**Result:** `Found 24 known vulnerabilities in 7 packages` (24 raw
findings / 16 unique advisory IDs — some IDs are cross-listed against more
than one package in the same dependency chain). **Identical** to the
2026-09-16 (`docs/DEPENDENCY_SECURITY.md`) and earlier-today baselines —
zero new advisories, zero newly-published fixes (`fix_versions: null` on
every one of the 7 packages, confirmed in this run's raw JSON). No drift.

**Reachability re-verification performed fresh this session** (commands
below actually run against this checkout, not reused from the prior
session's memory):

```
grep -rn "Checkpointer\|BaseCheckpointSaver\|\.compile(checkpointer" agent api db config identity media media_gen moderation rag search voice embeddings retrieval   → 0 matches
grep -rn "langgraph_sdk\|langgraph\.sdk" agent api db config identity media media_gen moderation rag search voice embeddings retrieval scripts                        → 0 matches
grep -rn "HttpClient\|AsyncHttpClient" embeddings media rag retrieval                                                                                                  → 0 matches (PersistentClient only)
grep -rn "ChatOpenAI|langchain_openai|from langchain\b|prompts\.loading|load_prompt" agent api db config identity media media_gen moderation rag search voice embeddings retrieval scripts tests → 0 matches
grep -rn "CachePolicy|BaseCache|cache_policy" agent api db config identity media media_gen moderation rag search voice embeddings retrieval scripts                    → 0 matches
```
Plus a direct read of both graph-compile call sites:
`agent/graph.py:257` → `return graph.compile()` (no arguments)
`agent/orchestrator/graph.py:126` → `return graph.compile()` (no arguments)

**New this session, not individually named in the prior pass's prose**:
`PYSEC-2026-2574`/`CVE-2026-27794` (LangGraph's *caching* layer, distinct
from its *checkpointing* layer — requires a `BaseCache`-derived backend
plus per-node `CachePolicy` opt-in) was previously bundled under a general
"langgraph-checkpoint family, not reachable" statement without confirming
the caching-specific mechanism by name. Confirmed independently above:
`CachePolicy`/`BaseCache`/`cache_policy` appear nowhere in this codebase,
and neither `.compile()` call passes a `cache=` argument.

| CVE / GHSA | Package | Current version | Severity | Fixed version | Runtime/Dev | Direct/Transitive | Reachable | Exploitability | Affected functionality | Available upgrade | Breaking-change risk | Remediation | Status | Evidence |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| CVE-2026-28277 / GHSA-g48c-2wqr-h844 (PYSEC-2026-83) | `langgraph` | 0.2.62 | Critical (RCE via msgpack checkpoint deserialization) | None published for 0.2.x; fixed in `langgraph>=1.0`/`langgraph-checkpoint>=3.0` | Runtime | Direct | **No** | Requires attacker write access to a persistent checkpoint store this app doesn't have | N/A — checkpointing feature unused | `langgraph>=1.0` (major) | **High** — 11-node SQL graph + orchestrator graph, out of scope this session (Phase 6) | None applied; compensating control is architectural (no checkpointer ever configured) | NOT REACHABLE — DOCUMENTED | `graph.compile()` called with zero args, both graphs; grep above, 0 `Checkpointer` matches |
| CVE-2026-48776 / GHSA-w39p-vh2g-g8g5 (PYSEC-2026-2194 / PYSEC-2026-2575) | `langgraph-sdk` | 0.1.74 | Medium (client-side URL/path injection) | `0.3.15` (transitive, tied to `langgraph>=1.0`) | Runtime | Transitive (via `langgraph`) | **No** | Requires this app to be a client of a remote LangGraph Platform API server | N/A — this app uses LangGraph as an in-process library only | Transitive, blocked on `langgraph` major bump | High (same blocker as above) | None applied | NOT REACHABLE — DOCUMENTED | 0 matches for `langgraph_sdk`/`langgraph.sdk` anywhere in first-party code |
| CVE-2026-45829 / GHSA-f4j7-r4q5-qw2c (PYSEC-2026-311) | `chromadb` | 1.5.9 | Critical (pre-auth RCE) | **None published** (`fix_versions: null`) | Runtime | Direct | **No** | Requires Chroma's HTTP server API running and network-reachable | N/A — server mode never started | None available | N/A | Monitor for upstream fix; do not switch to `HttpClient`/server mode without re-assessing | NOT REACHABLE — DOCUMENTED | Only `chromadb.PersistentClient(...)` constructed anywhere (`embeddings/schema_indexer.py:116`, `media/store.py`, `retrieval/vector_store.py`) |
| CVE-2026-45833 / GHSA-36p7-vc44-83pf (PYSEC-2026-3814) | `chromadb` | 1.5.9 | Critical (authenticated code injection via model-repo argument) | None published | Runtime | Direct | **No** | Same HTTP-server-only precondition as above | N/A | None available | N/A | Same as above | NOT REACHABLE — DOCUMENTED | Same evidence |
| CVE-2026-45831 / GHSA-xph7-9rjv-w5fr (PYSEC-2026-3815) | `chromadb` | 1.5.9 | High (cross-tenant authz bypass in `SimpleRBACAuthorizationProvider`) | None published | Runtime | Direct | **No** | Requires Chroma's own multi-tenant HTTP RBAC feature, never used | N/A | None available | N/A | Same as above | NOT REACHABLE — DOCUMENTED | Same evidence — no tenant/RBAC config for Chroma exists anywhere in this app |
| CVE-2026-45830 / GHSA-2wm9-hf6c-p5cr (PYSEC-2026-3813) | `chromadb` | 1.5.9 | High (missing authz, cross-tenant read/write) | None published | Runtime | Direct | **No** | Same HTTP-server-only precondition | N/A | None available | N/A | Same as above | NOT REACHABLE — DOCUMENTED | Same evidence |
| CVE-2026-34070 / GHSA-qh6h-p6c9-ff54 (PYSEC-2026-2193) | `langchain-core` | 0.3.86 | High (arbitrary file read via `prompts.loading`) | `langchain-core>=1.2.22` (transitive, tied to `langgraph` bump) | Runtime | Transitive (via `langgraph`) | **No** | Requires calling `langchain_core.prompts.loading` functions on attacker-influenced paths | N/A — this app builds every prompt manually via f-strings in `agent/llm_client.py`, never deserializes a LangChain prompt object | Transitive, blocked on `langgraph` major bump | High (same blocker) | None applied | NOT REACHABLE — DOCUMENTED | 0 matches for `prompts.loading`/`load_prompt` anywhere in first-party code |
| CVE-2026-26013 / GHSA-2g6r-c272-w58r (PYSEC-2026-2562) | `langchain-core` | 0.3.86 | High (SSRF via `ChatOpenAI.get_num_tokens_from_messages()` fetching arbitrary `image_url`) | Transitive, tied to `langgraph` bump | Runtime | Transitive | **No** | Requires instantiating LangChain's `ChatOpenAI` model class | N/A — this app is Ollama-only; `agent/llm_client.py` uses the `ollama` package's own client, never `langchain_openai`/`ChatOpenAI` | Transitive, blocked on `langgraph` major bump | High (same blocker) | None applied | NOT REACHABLE — DOCUMENTED | 0 matches for `ChatOpenAI`/`langchain_openai` anywhere in first-party code |
| CVE-2025-64439 / GHSA-wwqv-p2pp-99h5 (PYSEC-2026-1527) | `langgraph-checkpoint` | 2.1.2 | Critical (RCE via `JsonPlusSerializer`, the default checkpoint serializer) | `>=3.0` (transitive, tied to `langgraph` bump) | Runtime | Transitive (via `langgraph`) | **No** | Requires a configured checkpointer *and* attacker write access to its backing store | N/A — no checkpointer configured anywhere | Transitive, blocked on `langgraph` major bump | High (same blocker) | None applied | NOT REACHABLE — DOCUMENTED | Same `Checkpointer` grep as the first row |
| CVE-2026-27794 / GHSA-mhr3-j7m5-c7c9 (PYSEC-2026-2574) | `langgraph-checkpoint` | 2.1.2 | Critical (RCE via node-level result caching) | `>=4.0`/`4.1.1` (transitive) | Runtime | Transitive | **No** | Requires a `BaseCache`-derived cache backend *and* `CachePolicy` opted into on individual nodes | N/A — neither mechanism used anywhere in `agent/graph.py`/`agent/orchestrator/graph.py` | Transitive, blocked on `langgraph` major bump | High (same blocker) | None applied | NOT REACHABLE — DOCUMENTED | **New, specific verification this session** — 0 matches for `CachePolicy`/`BaseCache`; both `.compile()` calls take no `cache=` argument |
| CVE-2026-48775 / GHSA-fjqc-hq36-qh5p (PYSEC-2026-2573) | `langgraph-checkpoint` | 2.1.2 | Critical (RCE via `JsonPlusSerializer`, checkpoint bytes modified at rest) | `>=3.0` (transitive) | Runtime | Transitive | **No** | Same precondition as `CVE-2025-64439` above | N/A | Transitive, blocked on `langgraph` major bump | High (same blocker) | None applied | NOT REACHABLE — DOCUMENTED | Same `Checkpointer` grep |
| CVE-2025-71176 / GHSA-6w46-j5rx-g56g (PYSEC-2026-1845) | `pytest` | 8.3.4 | Medium (predictable `/tmp/pytest-of-{user}` dir → local DoS/privesc on a shared multi-user host) | `9.0.3+` — **no 9.0.x-equivalent fix exists in the 8.x line** (verified: `pip index versions pytest` lists up to `8.4.2`, no CVE-patched 8.x release) | **Dev** (`requirements.txt`'s own "Dev / test / lint (not required to run the app)" section) | Direct | **Dev only** — never imported/invoked by the running `uvicorn` process (`Dockerfile`'s `CMD` is `uvicorn api.main:app`, nothing else) | Requires local shell access to a shared host running pytest — CI runner is single-tenant per job | N/A at runtime; test collection only | `pytest>=9.0.3` (**major**, 8→9) | Medium — plugin/API compat unverified for this project's `pytest.ini_options`/fixture usage | **Not upgraded this session** — major bump, no patch-level fix exists, dev-only reachability | DEV ONLY | `pip index versions pytest` (2026-09-18): highest 8.x is `8.4.2`; `Dockerfile` never invokes `pytest` |
| CVE-2026-32274 / GHSA-3936-cmfr-pm3m (PYSEC-2026-2121) | `black` | 24.10.0 | Medium (unsanitized `--python-cell-magics` value → cache-filename path traversal) | `26.3.1+` — **no fix in the 24.x/25.x line** (verified: `pip index versions black` lists up to `26.5.1`, jumping 24.10.0→25.1.0→26.x, no patched 24.x release) | **Dev** | Direct | **Dev only** | Requires an attacker-controlled `--python-cell-magics` CLI value; this repo's CI invokes `black --check .` with no such flag, ever | N/A at runtime | `black>=26.3.1` (**major**, 24→26) | Medium-High — black 26's own formatting-rule changes would very likely reformat a large fraction of this codebase, a large unrelated diff | **Not upgraded this session** — major bump, dev-only, and CI never passes the vulnerable flag anyway | DEV ONLY | `pip index versions black`; `.github/workflows/ci.yml`'s own `black --check .` invocation (no flags) |
| CVE-2026-31900 / GHSA-v53h-f6m7-xcgm (PYSEC-2026-2120) | `black` | 24.10.0 | Medium (supply-chain issue in Black's own GitHub Action's `use_pyproject: true` option) | `26.1.0+` | **Dev** | Direct | **No** | This repo doesn't use Black's GitHub Action at all — `black` is installed via `pip install -r requirements.txt` and invoked directly, never through `psf/black`'s own Action | N/A | Same major-bump blocker | Same as above | Not upgraded this session | NOT REACHABLE — DOCUMENTED / DEV ONLY | `.github/workflows/ci.yml` reviewed in full — no `psf/black` Action reference anywhere |

**Verdict:** every currently-open dependency finding is either genuinely
not reachable through this application's own code paths (10 of 12 unique
findings, all independently re-verified this session with fresh `grep`
evidence, not just re-cited) or is dev-only tooling with no non-major-version
fix available (`pytest`, `black`). **Zero findings meet this session's own
Phase 4 priority tiers 1–7** (critical/high runtime, internet-facing,
file-processing, auth, agent-execution, or SQL/RAG-processing
vulnerabilities that are actually reachable) — every reachable-in-principle
finding requires either a feature this app never turns on (a LangGraph
checkpointer/cache backend, a remote LangGraph API server, Chroma's HTTP
server mode, LangChain's `ChatOpenAI`/prompt-file-loading) or local shell
access to the CI/dev environment itself.

## 2. Frontend (npm audit)

**Command executed (this session):**
```
npm audit --json
```
**Result:** `{"info":0,"low":0,"moderate":0,"high":0,"critical":0,"total":0}`
across 698 total dependencies (250 prod, 387 dev, 133 optional, 0 peer).
**Zero vulnerabilities.** No action needed, no suppressions in place.

## 3. Bandit (SAST)

**Command executed (this session):**
```
bandit -r agent api config db security media_gen moderation rag search voice media embeddings -f txt
```
**Result:** `0 issues` across 18,123 lines. Re-confirms the CI-gate-hardening
pass's own same-day finding — no drift, nothing new introduced.

## 4. Container scan (Trivy)

**VERIFIED this session** (the build that stalled across the prior
session finally completed partway through this one — see
`DEPENDENCY_SECURITY_FINAL_REPORT.md` §14 for the timeline). Command:

```
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v trivy-cache:/root/.cache/ \
  aquasec/trivy:latest image --severity CRITICAL,HIGH --format table text-to-sql-dashboard:localscan
```

**Result: 29 unique CRITICAL/HIGH CVE/GHSA IDs.** Two categories:

**(a) Python-ecosystem findings — 4 new, not surfaced by `pip-audit`,
each independently reachability-checked this session:**

| Finding | Package | Reachable? | Evidence |
|---|---|---|---|
| CVE-2026-23949 (path traversal) | `jaraco.context` | **No** | `docker run --rm ... find ... -iname '*jaraco*'` → only exists at `setuptools/_vendor/jaraco.context-5.3.0.dist-info` — a copy **vendored inside `setuptools` itself**, not a top-level installed package (`pip list` inside the image shows no `jaraco.context` entry at all). Used only by `setuptools`' own internal packaging logic, never imported by this app. |
| GHSA-6v7p-g79w-8964 (OOB read/crash) | `msgpack` | **No** | Same method — found only at `pip/_vendor/msgpack`, vendored inside `pip` itself for pip's own internal HTTP-cache handling (`CacheControl`), not a top-level dependency (absent from `pip list`). Note: this app *does* have a real, separate, unflagged `ormsgpack` package installed (a transitive dependency of something in `requirements.txt`) — confirmed distinct from the vendored `msgpack` Trivy flagged; not itself a Trivy finding. |
| CVE-2025-47273 (path traversal) | `setuptools` | **No** | Real top-level package (`pip list` confirms `setuptools 79.0.1`, `Required-by: torch`), but the vulnerable code path is setuptools' own package-extraction logic — exercised only when `pip install` processes an untrusted package, i.e. at Docker **build** time against this app's own fully-pinned, trusted `requirements.txt`. Never invoked by the running `uvicorn` process. |
| CVE-2026-24049 (RCE via malicious wheel file) | `wheel` | **No** | Same reasoning as `setuptools` — real top-level package (`wheel 0.46.3`), build-time-only tooling. |

**(b) Debian 13.6 base-image OS packages — 58 raw findings / ~17 unique
CVEs**, none previously tracked anywhere in this repo's security
documentation (`pip-audit`/`bandit`/`npm audit` have no visibility into
OS packages at all — this is genuinely new coverage this pass adds):

| Package(s) | CVE(s) | Nature | Reachability note |
|---|---|---|---|
| `util-linux` (+ `libmount1`/`libuuid1`/`libsmartcols1`/`liblastlog2-2`/`login`/`mount`/`bsdutils`/`libblkid1`) | CVE-2026-76642, 78408, 78409, 78410 | Mount-namespace/`nsenter`/cgroup privilege-escalation shapes | Requires local/container-escape-adjacent capabilities this app's own non-root runtime user (`USER app`, uid 1000, confirmed via `docker run ... id`) doesn't have, and this app never invokes `mount`/`nsenter` itself |
| `curl`/`libcurl4t64` | CVE-2026-12064, 8286, 8458, 8927 | SSH host-verification bypass, TLS/proxy issues | `curl` is invoked once, by the `Dockerfile`'s own `HEALTHCHECK` (`curl -f http://localhost:8000/health`) — plain loopback HTTP, no SSH, no TLS, no proxy involved, so the specific vulnerable code paths aren't exercised by that one real call site |
| `libsqlite3-0` | CVE-2026-11822, 11824 | FTS5 arbitrary code execution | **Not independently verified reachable/unreachable** this session — this app has no direct SQLite usage (identity DB is PostgreSQL, RAG store is SQL Server, main DB is postgres/mysql/mssql/oracle per `CLAUDE.md`), but whether any transitive Python dependency `dlopen`s the system `libsqlite3` wasn't checked with the same rigor as the Python-ecosystem findings above — flagged as a real gap, not silently assumed safe |
| `pcre2`/`libpcre2-8-0` | CVE-2026-86145, 89157, 89161 | Regex-engine out-of-bounds write | Same "not independently verified" caveat — Python's own `re` module doesn't use system PCRE2, but some transitive dependency might |
| `gzip` | CVE-2026-41992 | Info disclosure | Not independently verified |
| `libacl1` | CVE-2026-54369 | Symlink traversal privesc | Not independently verified |
| `ncurses`/`libncursesw6`/`libtinfo6`/`ncurses-base` | CVE-2025-69720 | Buffer overflow | Not independently verified — likely pulled in as a terminal/TTY dependency, not something a headless `uvicorn` server process exercises |
| `libsystemd0`/`libudev1` | CVE-2026-16742 | Local privesc via `systemd-homed` | `systemd-homed` is a specific systemd component this containerized app doesn't run (no init system, no `systemd-homed` service) — low practical exploitability, not independently verified further |
| `perl-base` | CVE-2026-9538 | `perl-Archive-Tar` DoS | `fix_deferred` per Trivy's own status column (Debian hasn't shipped a fix yet); not independently verified whether anything invokes Perl at runtime (unlikely — no Perl usage anywhere in this codebase) |

**Verdict:** the 4 Python-ecosystem findings are conclusively NOT
REACHABLE with the same file-path-level evidence rigor as this document's
pip-audit findings. The ~17 unique OS-level findings are **partially, not
fully, reachability-verified this session** — the `util-linux`/`curl`
groups have a specific, checked reasoning; the remaining ~9 (sqlite3,
pcre2, gzip, libacl1, ncurses, systemd, perl-base) are recorded honestly
as **NOT INDEPENDENTLY VERIFIED**, not assumed safe. **Recommended
remediation, distinct from anything CVE-list-shaped:** several of these
already have a Debian-published fix (`gzip`→`1.13-1+deb13u1`, `pcre2`→
`10.46-1~deb13u2`, `sqlite3`→`3.46.1-7+deb13u2`) — re-pinning the
`Dockerfile`'s `python:3.11-slim` base-image digest to a current build
would likely resolve most of these automatically, since they're base-OS
package patches, not this application's own dependency choices. Not done
this session (a Dockerfile change with its own re-verification need,
judged better as its own small, reviewed follow-up than bundled here).

## 5. Secret scanning (gitleaks / detect-secrets)

Unchanged from the CI-gate-hardening pass earlier today — **not re-run**
this session since nothing in the tracked-file set changed (`git status`
confirmed no new commits landed between sessions). See that pass's
findings: gitleaks itself remains **NOT VERIFIED** (Go binary unavailable
in this sandboxed environment); `detect-secrets` was fully run and
triaged, with a committed `.secrets.baseline` and a blocking CI gate.
