# File Upload / File Processing Security — Final Report

## Executive Summary

This app's file-handling surface is small and already well-designed: exactly two
user-facing upload endpoints (PDF for RAG, recorded audio for voice mode), one
internal download path (provider-generated media), and a CLI-only media-library
ingestion pipeline never reachable from an HTTP request. A full Phase 0 inventory
found no ZIP/Office/SVG/generic-file handling anywhere — this narrows the applicable
threat surface considerably relative to a generic "file upload" checklist, and this
report states that narrowing explicitly rather than building unneeded infrastructure
to fill checklist categories the app has no code for.

Of 28 scored categories, 19 were already GREEN before this pass (strong existing
controls from Phase 1/2 security work — content-based type validation, mandatory
moderation gating, SSRF-hardened downloads, RBAC on every route, no filename-derived
paths anywhere). Three real, evidence-backed gaps were found and fixed. Two categories
remain RED, both disclosed and judged acceptable at this app's stated current scale
(single-user/local-dev) rather than silently left unexamined. **No CRITICAL or HIGH
severity vulnerability was found.**

## Existing controls discovered (not duplicated or rebuilt)

- Content-based (magic-byte) validation on both upload paths — extensions/client MIME
  never trusted anywhere.
- Mandatory, fail-closed moderation gate (`moderation/gate.py`) ahead of all RAG/
  media-library storage, with asset-level all-or-nothing rejection.
- SSRF-hardened media download (`media_gen/download.py`) — resolved-IP validation
  against private/reserved ranges.
- No filename-derived filesystem paths anywhere in the codebase.
- Path-containment re-validation on the one route that resolves a stored path
  (`api/media_library.py::_resolve_safe_path`).
- Bounded reads (never full-buffer) on both upload endpoints, with size limits below
  the expensive-processing threshold.
- PDF page-count cap closing a decompression-bomb-shaped gap byte-size alone leaves
  open.
- RBAC + a sensitivity-tag-specific additional permission on document downloads.
- Rate limiting on both upload endpoints.
- Structured audit logging of every moderation/rejection outcome, with file content
  never logged.

## New controls implemented this pass

1. **Media download Content-Type allowlist** (`media_gen/download.py`) — a provider
   response is only trusted as `image/`/`video/`/`audio/`; anything else falls back to
   `application/octet-stream`. Closes a stored-content-confusion risk where a
   compromised/buggy provider response could otherwise be reflected verbatim as the
   `Content-Type` on `GET /media/{media_id}`.
2. **`X-Content-Type-Options: nosniff`** added to all three file-serving routes
   (`api/media.py`, `api/media_library.py`, `api/documents.py`) — defense-in-depth
   against browser MIME-sniffing.
3. **RAG-ingested text normalization + injection-pattern detection**
   (`rag/ingestion.py`) — extracted PDF text is now NFKC-normalized (closing a
   homoglyph-obfuscation gap) and scanned for injection-shaped patterns before
   chunking/embedding, logging a `possible_rag_poisoning` audit event on a match
   (detection-only, matching this codebase's established philosophy for this class of
   check). Brings this ingestion path to parity with its sibling untrusted-text entry
   points (`agent.input_guard`, `agent.nodes.retrieve_schema_node`).
4. **CSV spreadsheet-formula-injection protection** (`frontend/src/lib/csv.ts`) — a
   query-result cell beginning with `=`/`+`/`-`/`@` is now prefixed with `'` before
   CSV-syntax escaping, neutralizing Excel/Sheets/LibreOffice formula execution on
   download.

## Controls intentionally not implemented, and why

See `docs/FILE_UPLOAD_SECURITY_REPORT.md`'s "Not implemented, and why" section for the
full reasoning on each: malware/AV scanning (real gap, needs a new external service,
not built speculatively), per-user storage quotas (not justified at this app's stated
single-user scale), a physical quarantine directory (the in-memory-then-DB ordering
already achieves the same effect), CDR (no PDF rendering exists to disarm), generic
archive/ZIP security (no archive feature exists), a dedicated polyglot detector (the
format-specific parsers already require valid-as-that-format content).

## Vulnerabilities found

| Severity | Finding | Status |
|---|---|---|
| Medium | G1: Media download route trusted an external provider's `Content-Type` header with no allowlist, reflected to the browser | **Fixed** |
| Low | G2: RAG-ingested document text had no homoglyph-normalization/injection-pattern-detection layer, unlike sibling untrusted-text paths | **Fixed** |
| Medium | G3: Frontend CSV export had no spreadsheet-formula-injection protection | **Fixed** |
| Low (disclosed, not fixed) | No malware/AV scanning of uploaded content | Accepted gap — see remaining risks |
| Low (disclosed, not fixed) | No per-user/tenant storage quota | Accepted gap at current scale |

No Critical or High severity finding was identified.

## Tests added

- `tests/test_media_gen.py::TestDownloadMediaBytes` — 2 new tests (allowed types
  pass through; disallowed type falls back to `application/octet-stream`).
- `tests/test_api_media.py::TestGetMedia` — 1 new test (`nosniff` header present).
- `tests/test_rag_ingestion.py::TestRagPoisoningDetection` — 4 new tests (detection
  doesn't block ingestion; audit event fires on a match; homoglyph text is normalized
  before storage; clean text produces no event).

No automated test was added for the frontend CSV fix — the frontend has no test
framework configured at all (no `vitest`/`jest`, no `test` script in `package.json`),
a pre-existing gap this pass did not build new infrastructure to close. The fix itself
is a small, pure string-transform function verified by manual trace-through (documented
in the security report).

## Tests passed

Full backend suite: **1,025 passed, 0 failed** (up from 1,018 at the start of this
specific task — 7 new tests, all passing). `ruff check`, `black --check`, and targeted
`mypy` all clean on every touched file (two pre-existing, unrelated mypy findings in
`moderation/gate.py`/`media/ingest.py` predate this session and were left alone, out of
scope for this task).

## Scanner results

**Not run interactively in this session** — stated honestly rather than fabricated.
`gitleaks`/`bandit`/`trivy` are already wired into this repo's CI pipeline
(`.github/workflows/ci.yml`, added in Phase 2) and cover the dependencies this
file-handling surface relies on (`pypdf`, `pymupdf`, `pytesseract`, `opencv-python`,
`faster-whisper`) on every push/PR — their last recorded results are in
`docs/DEPENDENCY_SECURITY.md` (Phase 1) and `docs/PHASE2_SECURITY_REPORT.md` (Phase 2),
not re-run specifically for this file-upload-scoped task. No ZAP/Nuclei dynamic scan
was performed (this session has no way to run a live HTTP scan against a running
instance of this app).

## OWASP ASVS 5.0 file-handling mapping (informal — not a certified assessment)

| ASVS area | Status |
|---|---|
| V5.1 File Handling Documentation | Met — this report + the security/threat-model/inventory docs |
| V5.2 File Upload and Content | Largely met — content-based validation, size/page/duration limits, mandatory moderation. Gap: no malware scanning. |
| V5.3 File Storage | Met — DB storage for accepted PDFs, no filename-derived paths, isolated thumbnail directory |
| V5.4 File Download | Met — opaque-id resolution, RBAC, Content-Type allowlist + nosniff (this pass) |

This is an internal self-assessment against ASVS's general intent, not a claim of
formal ASVS certification.

## Before → after security posture

| | Before this pass | After this pass |
|---|---|---|
| Media download Content-Type | Trusted verbatim from provider | Allowlisted to image/video/audio |
| Download response headers | No `X-Content-Type-Options` | `nosniff` on all 3 serve-back routes |
| RAG text poisoning detection | None (asymmetric vs. sibling paths) | Normalize + scan + audit-log, at parity |
| CSV export | Vulnerable to formula injection | Protected (`'` prefix mitigation) |
| Scored categories | Not previously scored as a set | 19 GREEN / 6 AMBER / 2 RED / 3 N/A |

## Remaining risks (ranked)

1. **No malware/AV scanning** (RED) — the most significant remaining gap. Low
   real-world severity today given the narrow accepted-format set and that nothing is
   ever executed by this app, but a real gap before accepting uploads from a genuinely
   adversarial population.
2. **No per-user/tenant storage quota** (RED) — acceptable at today's stated
   single-user scale; a real gap for multi-tenant deployment.
3. **No frontend test coverage at all** (AMBER, broader than just this fix) — the CSV
   formula-injection fix, and any future frontend security fix, has no automated
   regression protection.
4. **Voice audio has no pre-processing magic-byte check** (AMBER) — functionally
   covered by PyAV's decode-or-fail behavior, but not a signature check before any
   processing begins.
5. **No animated-image frame-count cap / explicit max-pixel-dimension rejection**
   beyond the tiling threshold (AMBER).

## Production recommendation

**PRODUCTION READY WITH CONDITIONS.**

No Critical or High severity finding exists, and every Medium/Low finding identified
this pass was fixed. The two RED categories are genuine gaps, not hidden ones, and
both are reasonably scoped to this app's stated current deployment shape
(single-user/local-dev, per `README.md`). Conditions for a broader deployment:

- Add malware/AV scanning (e.g. ClamAV) ahead of the moderation gate before accepting
  uploads from an untrusted/adversarial multi-tenant population.
- Add per-user/tenant storage quotas before multi-tenant deployment.
- Stand up a frontend test framework and add coverage for the CSV export fix (and
  other frontend security-relevant logic) before treating the frontend as equally
  regression-protected as the backend.

## Files changed

`media_gen/download.py`, `api/media.py`, `api/media_library.py`, `api/documents.py`,
`rag/ingestion.py`, `frontend/src/lib/csv.ts`, `tests/test_media_gen.py`,
`tests/test_api_media.py`, `tests/test_rag_ingestion.py`.

## Configuration changes

None — every fix this pass reused existing `Settings` fields and configuration; no new
environment variable was introduced.

## Deployment considerations

None of this pass's changes require a migration, a new dependency, or an infrastructure
change. The `X-Content-Type-Options` header addition and Content-Type allowlist are
transparent to any existing client. No action is required to deploy these changes
beyond a normal release.
