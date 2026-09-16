# Dependency Security — Phase 1 (2026-09-16)

Supersedes `docs/RISK_REGISTER.md`'s R-010 entry (2026-09-13, "65 advisories / 10 packages") as the current, live-verified figure. That entry is still accurate as a historical record and is left in place; this document is the authoritative current state.

## Method

- `pip-audit -r requirements.txt --desc --format json` (Python, full dependency resolution — this covers transitive dependencies pulled in by resolving `requirements.txt`, not just the packages pinned directly in it).
- `npm audit --json` (frontend, `frontend/package-lock.json`).
- Manual reachability analysis for every finding: grepped the actual call sites in this codebase to determine whether the vulnerable code path is ever exercised by this app's own usage, rather than assuming CVE severity equals exploitability here.
- GitHub Actions pinning reviewed (`.github/workflows/ci.yml`).
- Docker base images reviewed (`Dockerfile`) — digest-pinned already; no container/OS-package scan (Trivy/Grype) was available in this environment, flagged as a Phase 2 follow-up, not silently skipped.

## Summary

| | Before this pass | After this pass |
|---|---|---|
| pip-audit unique advisories | 34 (9 packages) | 15 (7 packages) |
| npm audit vulnerabilities | 0 | 0 |
| Packages upgraded | — | `pillow` 11.3.0→12.3.0, `python-dotenv` 1.0.1→1.2.2 |
| Regression check | — | Full 936-test suite green after both upgrades |

No P0-severity finding (RCE/auth-bypass/SSRF/deserialization/privesc/secret-disclosure that is *remotely exploitable in this app's actual deployment*) was found to be currently reachable. Two findings are nominally P0-shaped by CVE category (`chromadb`'s pre-auth RCE, `langgraph`'s checkpoint deserialization) but were verified **not reachable** through this app's own code paths — see below. Both are still tracked and flagged for monitoring, since "not reachable today" is a property of this app's current configuration, not a permanent guarantee.

---

## Findings

### chromadb 1.5.9 — 4 advisories, NOT REACHABLE in this app's deployment

| Field | Value |
|---|---|
| Package | `chromadb` |
| Installed / pinned version | 1.5.9 |
| Direct or transitive | Direct (`requirements.txt`) |
| Advisories | PYSEC-2026-311 (CVE-2026-45829, pre-auth code injection via `trust_remote_code` on the collections API), PYSEC-2026-3814 (CVE-2026-45833, authenticated code injection, same mechanism), PYSEC-2026-3815 (CVE-2026-45831, cross-tenant authorization bypass in `SimpleRBACAuthorizationProvider`), PYSEC-2026-3813 (CVE-2026-45830, missing authorization allows cross-tenant read/write) |
| Severity (by CVE nature) | Critical (RCE), High (authz bypass) — see exploitability below for why this app's actual exposure is much lower |
| Fixed version | **None published yet** (`fix_versions: []` for all four as of this scan) |
| Affected code path | Chroma's HTTP server API (`/api/v2/tenants/{tenant}/databases/{db}/collections`, `SimpleRBACAuthorizationProvider`) |
| Exploitability in this app | **Not reachable.** Verified by grep: this app only ever constructs `chromadb.PersistentClient(...)` (`embeddings/schema_indexer.py::get_chroma_client`, `media/store.py`) — an embedded, in-process, no-network-server client. It never calls `chromadb.HttpClient`, never runs a Chroma server process, and never exposes the Chroma HTTP API to any network caller. All four advisories are specific to that HTTP server surface, which this deployment never instantiates. |
| Remediation | No upstream fix exists to apply yet. **Do not** switch this app to `HttpClient`/a standalone Chroma server without re-assessing these advisories first. Monitor for an upstream patched release; re-run `pip-audit` on the next dependency pass. |

### langgraph 0.2.62 + langgraph-checkpoint 2.1.2 + langgraph-sdk 0.1.74 + langchain-core 0.3.86 — 8 advisories, NOT REACHABLE

| Field | Value |
|---|---|
| Package | `langgraph` (direct, `requirements.txt`) and its transitive deps `langgraph-checkpoint`, `langgraph-sdk`, `langchain-core` |
| Advisories | PYSEC-2026-83 / CVE-2026-28277 (langgraph checkpoint msgpack deserialization → potential RCE, requires attacker write access to the checkpoint store), PYSEC-2026-2194 / CVE-2026-48776 (langgraph-sdk unsafe URL path construction from unsanitized identifiers), plus 3 more in `langgraph-checkpoint` (deserialization-adjacent) and 2 in `langchain-core` |
| Severity (by CVE nature) | Critical/High (deserialization RCE), Medium (URL path injection) |
| Fixed version | `langgraph` → 1.0.10 / `langgraph>=1.0`; `langgraph-sdk` → 0.3.15; `langgraph-checkpoint` → 3.0.0/4.0.0/4.1.1 (across the 3 separate advisories) |
| Affected code path | A configured `BaseCheckpointSaver` (persistent checkpointing) for the deserialization issues; the `langgraph_sdk` client connecting to a remote LangGraph API server for the URL-path issue |
| Exploitability in this app | **Not reachable.** Verified by grep across `agent/`: no `Checkpointer`/`checkpoint` usage anywhere outside test files — `agent/graph.py::build_graph()` compiles and invokes the graph statelessly (`.invoke()` per call, per-question state passed as a plain dict, no persistence layer). `langgraph_sdk` is never imported anywhere in this codebase (grepped) — this app uses LangGraph as an in-process library, never LangGraph Platform/a remote API server. Both prerequisite conditions for these CVEs (a persistent checkpoint store an attacker can write to; a connection to a remote LangGraph API server) are simply absent from this deployment. |
| Remediation | **Deliberately not upgraded in this pass.** `langgraph` 0.2.x→1.x is a major version bump with real breaking-change risk to the core orchestration engine (11-node graph, the multi-source orchestrator's own graph, `agent/complexity.py`'s retry-budget integration) — upgrading it blindly would violate this pass's "no large rewrite" / "preserve existing functionality" constraints, and the vulnerabilities aren't reachable today regardless. Recommended as a **Phase 2** item: a dedicated upgrade pass with its own full regression pass against the live benchmark (not just the mocked unit suite), scheduled deliberately rather than folded into this Phase 1 pass. |

### pillow 11.3.0 — 18 unique advisories, NOT REACHABLE through this app's ingestion path — UPGRADED ANYWAY

| Field | Value |
|---|---|
| Package | `pillow` |
| Installed version before / after | 11.3.0 → **12.3.0** |
| Direct or transitive | Direct (`requirements.txt`) |
| Advisories | 18 unique CVEs, all decompression-bomb / out-of-bounds-write / memory-corruption issues in specific Pillow plugins: PSD (CVE-2026-25990, CVE-2026-42311), FITS (CVE-2026-40192), PCF/BDF font loading (CVE-2026-54059, CVE-2026-55379, CVE-2026-54060), GD images (CVE-2026-55380), McIdas AREA (CVE-2026-54058), font-metrics integer overflow (CVE-2026-42308), coordinate-API heap overflow (CVE-2026-59199), ImageCms heap corruption (CVE-2026-59205), plus others in the same family |
| Severity | High (several: memory corruption / OOB write with a low-byte-count trigger file) |
| Fixed version | 12.3.0 (covers all 18; a few of the earlier ones were already fixed by 12.1.1/12.2.0, superseded by the final 12.3.0 pin) |
| Affected code path | `PIL.Image.open()` on an untrusted file (PSD, FITS), or direct use of `PIL.GdImageFile`/`PIL.BdfFontFile`/`PIL.PcfFontFile`/`PIL.McIdasImagePlugin` |
| Exploitability in this app | **Not reachable, verified by code read.** Every `Image.open()` call site in this app (`media/ingest.py:172,205`, `media/embedding.py:68`, `media/ocr.py:31`) only ever runs on bytes that already passed `media/ingest.py::_sniff_media_type`'s magic-byte allowlist — restricted to JPEG (`\xff\xd8\xff`), PNG, GIF87a/89a, BMP, and WEBP (via RIFF). None of the vulnerable formats' magic bytes (PSD `8BPS`, FITS `SIMPLE`, GD, PCF, BDF) are in that allowlist, so a file in any of the vulnerable formats is rejected before it ever reaches `Image.open()`. The `GdImageFile`/`BdfFontFile`/`PcfFontFile` classes specifically (the worst of the 18 — reachable only via their own direct constructors, not `Image.open()`'s normal auto-detect path) are never imported or called anywhere in this codebase at all. |
| Remediation applied | **Upgraded to 12.3.0** anyway — same public API for this app's actual usage (`Image.open`, `.resize`, `.convert`, `.save`, all unchanged), zero test regressions after the bump (full 936-test suite reran clean), and it closes the gap defensively in case `_sniff_media_type`'s allowlist is ever loosened later without someone re-checking this analysis. Low-risk, high-value bump — the kind of "upgrade safely" the Phase 1 brief calls for, as opposed to `langgraph` above. |

### python-dotenv 1.0.1 — 1 advisory, NOT REACHABLE — UPGRADED ANYWAY

| Field | Value |
|---|---|
| Package | `python-dotenv` |
| Installed version before / after | 1.0.1 → **1.2.2** |
| Direct or transitive | Direct (`requirements.txt`), also a transitive dependency of `pydantic-settings` (used to load `.env`) |
| Advisory | PYSEC-2026-2270 / CVE-2026-28684 — `set_key()`/`unset_key()` follow symlinks when rewriting a `.env` file; a local attacker who can pre-place a symlink at the `.env` path can get an arbitrary file overwritten when those functions are called |
| Severity | Medium (requires local filesystem access and a specific cross-device-mount configuration; not remotely exploitable) |
| Fixed version | 1.2.2 |
| Affected code path | `dotenv.set_key()` / `dotenv.unset_key()` |
| Exploitability in this app | **Not reachable.** Grepped the whole codebase for `set_key`/`unset_key` — this app only ever *reads* `.env` (via `pydantic_settings.BaseSettings`'s own loading, `config/settings.py`), never writes to it programmatically. |
| Remediation applied | **Upgraded to 1.2.2** — same major version line, low regression risk, full test suite reran clean. |

### pytest 8.3.4, black 24.10.0 — dev-tooling only, deferred

| Field | Value |
|---|---|
| Packages | `pytest` (1 advisory: PYSEC-2026-1845/CVE-2025-71176, fix in 9.0.3), `black` (2 advisories: PYSEC-2026-2121/CVE-2026-32274, PYSEC-2026-2120/CVE-2026-31900, fix in 26.3.x) |
| Severity | Low in this context |
| Exploitability in this app | **Not reachable in any deployed environment.** Both are dev-time/CI-only tooling — never installed or invoked in the production Docker image's runtime path (the `Dockerfile` installs from `requirements.txt` and runs `uvicorn`, never `pytest`/`black`), only in developer machines and the CI runner. |
| Remediation | **Deliberately deferred.** Both are major-version bumps (`pytest` 8→9, `black` 24→26); `black` 26 in particular would very likely reformat a large fraction of the codebase (its own formatting-rule changes across major versions), which is exactly the kind of large, security-unrelated diff this pass's "no large rewrite" constraint is meant to avoid. Recommended as a low-priority Phase 2 cleanup item, done as its own reviewed change (and, for `black`, its own dedicated reformatting commit) rather than folded in here. |

---

## Frontend (npm)

`npm audit` on `frontend/package-lock.json`: **0 vulnerabilities** (info/low/moderate/high/critical all zero). No action needed. No CI gate change needed either — nothing to gate.

## GitHub Actions

`.github/workflows/ci.yml` uses `actions/checkout@v4`, `actions/setup-python@v5`, `actions/setup-node@v4` — official, GitHub-maintained actions, tag-pinned (not SHA-pinned). This is standard, reasonably low-risk practice (unlike a third-party action, where SHA-pinning is a stronger recommendation), inconsistent only in the sense that the `Dockerfile`'s base images *are* SHA/digest-pinned for the same class of "don't trust a mutable tag" reasoning. Recommended as a **quick-win, Phase 2** item: pin these three to a specific commit SHA to match the Dockerfile's own standard, not because a specific compromise is known, just for consistency with this project's already-stated supply-chain posture.

## Docker base images

Both `Dockerfile` stages are already digest-pinned (`python:3.11-slim@sha256:...`, `node:22-slim@sha256:...`) — no floating-tag risk. No OS-package (`apt`)-level vulnerability scan (Trivy/Grype) was run against the built image in this pass — no such tool was available in this sandboxed environment, and building+scanning the full image was judged out of scope for this pass's time budget. **Flagged, not silently skipped**: recommended as a Phase 2 CI addition (`trivy image` or `grype` against the built image, alongside the existing `pip-audit` step).

## Recommendation summary

| Priority | Action |
|---|---|
| Done (this pass) | Upgrade `pillow` → 12.3.0, `python-dotenv` → 1.2.2 |
| P1, Phase 2 | Dedicated `langgraph` 0.2→1.x upgrade pass, its own full regression cycle including the live benchmark |
| P2, Phase 2 | Add a container image scan (Trivy/Grype) to CI; SHA-pin GitHub Actions |
| P3, Phase 2 | `pytest`/`black` major-version bumps, done as their own isolated commits |
| Ongoing | Re-run `pip-audit` before every dependency-bump pass; monitor for a `chromadb` fix (currently none published) before ever considering `HttpClient`/server mode |
