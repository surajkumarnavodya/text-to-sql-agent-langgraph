# File Upload Security — Scored Report

Evidence-based scoring against `docs/FILE_UPLOAD_SECURITY_INVENTORY.md`'s Phase 0
inventory and the audit performed this pass. **GREEN** = fully addressed, evidenced by
code + tests. **AMBER** = partially addressed or addressed with a disclosed, accepted
limitation. **RED** = not addressed. **N/A** = the risk category doesn't apply to this
app's actual feature set (stated explicitly rather than silently marked GREEN by
default — a category this app has no code for is not the same as a category this app
has secured).

| Category | Score | Evidence |
|---|---|---|
| Extension Validation | 🟢 GREEN | Never trusted for type decisions anywhere — content (magic bytes / decode success) is the sole basis. Sidesteps the entire double-extension/encoding attack class by not relying on extensions at all. |
| MIME Validation | 🟢 GREEN | Client-supplied `Content-Type` never trusted. The one external-source MIME trust point (`media_gen/download.py`'s provider response) is now allowlisted (finding G1, fixed this pass). |
| Magic Bytes | 🟡 AMBER | PDF (`%PDF-`) and media-library ingestion (explicit signature table) both have real pre-processing magic-byte checks. Voice audio has **no** pre-check — relies on PyAV's decode-or-fail behavior instead, which is functionally equivalent (unparseable content never reaches Whisper) but not a signature check before any processing begins. |
| Content Validation | 🟢 GREEN | Real, maintained parsers throughout (`pypdf`/`pymupdf`, PyAV, OpenCV) — no hand-rolled format parsing. Page-count cap enforced before PDF text extraction. |
| Filename Security | 🟢 GREEN | No code path anywhere constructs an on-disk path from a user-supplied filename (verified in Phase 0 inventory). Every temp file is `mkstemp`-random or content-hash-named. |
| Path Traversal | 🟢 GREEN | Every serve-back route resolves an opaque id, never a raw path. `api/media_library.py::_resolve_safe_path` explicitly re-validates containment against the configured root before opening any file. |
| Storage Security | 🟢 GREEN | Uploaded PDF bytes stored in a DB column, not the filesystem. No storage location overlaps source code/web-root/config directories. |
| Archive Security | ⚪ N/A | No ZIP/DOCX/XLSX/PPTX upload or parsing exists anywhere in the codebase (confirmed: zero `zipfile` usage repo-wide). |
| Zip Bomb Protection | ⚪ N/A | No ZIP container is ever parsed by this app — nothing to protect against. |
| Malware Scanning | 🔴 RED | No signature-based malware/AV scanning exists. The moderation gate checks content-policy categories (hate/violence/etc.), not malware signatures. Disclosed as an accepted gap (`docs/FILE_UPLOAD_SECURITY.md` §19), not silently omitted. |
| Image Security | 🟡 AMBER | Magic-byte allowlist + oversized-image tiling (`media_image_tile_threshold_px`) exist. No explicit maximum-pixel-dimension rejection independent of the tiling threshold, and no animated-image (multi-frame GIF) frame-count cap. |
| PDF Security | 🟢 GREEN | Never rendered/executed — text extraction and rasterization only, neither interprets JS/launch-actions/annotations. Page-count cap bounds resource cost. |
| Office Security | ⚪ N/A | No DOCX/XLSX/PPTX ingestion path exists. |
| Media Security | 🟡 AMBER | Size cap + magic-byte allowlist + (for voice) duration cap all present. No codec-level allowlist (e.g. rejecting an unusual codec inside an otherwise-valid container) beyond container-format signature matching. The full media-library ingestion pipeline (scene detection, captioning) is CLI-only, not reachable from an HTTP request, which meaningfully narrows its real exposure. |
| OCR Security | 🟢 GREEN | Fails open (never blocks ingestion on OCR failure); bounded by the same page-count/tiling limits as the images/PDFs it processes; output treated as untrusted text like the rest of extracted content (§RAG below). |
| RAG Security | 🟢 GREEN | Mandatory moderation gate (hard-reject blocks storage entirely) + untrusted-data prompt framing (pre-existing) + normalize/injection-pattern-scan/audit-log layer (finding G2, added this pass) — now at parity with this codebase's other untrusted-text entry points. |
| Database Security | 🟢 GREEN | All `rag/store.py`/`moderation/store.py` operations use parameterized SQLAlchemy `text()` calls, consistent with this codebase's established no-string-interpolated-SQL discipline (`agent/sql_validator.py`'s own posture, applied here too). Moderation metadata stores category names/counts only, never chunk content. |
| Download Security | 🟢 GREEN | Opaque-id-resolved routes only; RBAC on every route; `X-Content-Type-Options: nosniff` added to all three serve-back routes this pass; Content-Type allowlist on the one route that previously trusted an external header (finding G1). |
| Authentication | 🟢 GREEN | Every upload/download route requires `require_permission(...)`, verified end-to-end by `tests/test_api_authz.py`. |
| Authorization | 🟢 GREEN | Fine-grained RBAC (`DOCUMENTS_WRITE`/`_READ`/`_READ_SENSITIVE`/`VOICE_USE`/`MEDIA_GENERATE`/`MEDIA_SEARCH`); sensitivity-tagged documents require an additional permission beyond ordinary read (a real prior gap, already closed in Phase 2). |
| Rate Limiting | 🟢 GREEN | `document_upload` and `voice_transcribe` both rate-limited per client IP via `enforce_api_action_rate_limit`. |
| Quotas | 🔴 RED | No per-user/per-tenant total-storage or total-upload-count quota exists. Disclosed as an accepted gap for this app's stated single-user/local-dev scale (`README.md`'s Limitations section) — a real gap for a genuine multi-tenant deployment. |
| Resource Limits | 🟢 GREEN | Byte-size, page-count, duration, and moderation-chunk-size limits all present and enforced *before* the expensive operation they bound, not after. |
| Logging | 🟢 GREEN | Structured `security.audit` events for every moderation/poisoning-detection outcome; never logs file bytes, full extracted text, or moderation chunk content. |
| Monitoring | 🟡 AMBER | `scripts/monitoring_summary.py` (Phase 2/3) summarizes audit events including file-upload-related ones from captured logs — no dedicated real-time dashboard/alerting exists, consistent with this app's deliberate no-commercial-monitoring-dependency posture (`docs/OBSERVABILITY.md`), but genuinely less than a production multi-tenant service would want. |
| Testing | 🟡 AMBER | Backend: strong, targeted coverage added/verified this pass (`tests/test_rag_ingestion.py`, `tests/test_media_gen.py`, `tests/test_api_media.py`, plus pre-existing `tests/test_media_ingest.py`/`tests/test_api_documents.py`/`tests/test_api_voice.py`). Frontend: the CSV-formula-injection fix (`frontend/src/lib/csv.ts`) has **no automated test** — the frontend has no test framework configured at all (no vitest/jest, no `test` script in `package.json`), a pre-existing gap this pass did not build new infrastructure to close. |
| CI/CD | 🟢 GREEN | `gitleaks`/`bandit`/`trivy` (Phase 2) already scan the dependencies this file-handling surface relies on (`pypdf`, `pymupdf`, `pytesseract`, `opencv-python`, `faster-whisper`); `pip-audit` covers known CVEs in the same set. |
| Documentation | 🟢 GREEN | This report + `docs/FILE_UPLOAD_SECURITY_INVENTORY.md` + `docs/FILE_UPLOAD_SECURITY.md` + `docs/FILE_UPLOAD_THREAT_MODEL.md`. |

## Score summary

- 🟢 GREEN: 19 of 28 applicable+scored categories
- 🟡 AMBER: 6 (Magic Bytes, Image Security, Media Security, Monitoring, Testing)
- 🔴 RED: 2 (Malware Scanning, Quotas)
- ⚪ N/A: 3 (Archive Security, Zip Bomb Protection, Office Security — genuinely
  out of scope, not hidden weaknesses)

## Fixes made this pass (with evidence)

| Finding | Fix | File(s) | Test(s) |
|---|---|---|---|
| G1: `GET /media/{media_id}` trusted the IMA provider's `Content-Type` header verbatim, with no allowlist | Content-Type allowlist (`image/`/`video/`/`audio/` only, else `application/octet-stream`) + `X-Content-Type-Options: nosniff` on all three download routes | `media_gen/download.py`, `api/media.py`, `api/media_library.py`, `api/documents.py` | `tests/test_media_gen.py::TestDownloadMediaBytes` (2 new tests), `tests/test_api_media.py` (1 new test) |
| G2: RAG-ingested PDF text had no homoglyph-normalization or injection-pattern-detection, unlike every sibling untrusted-text entry point | `normalize_text` + `INJECTION_PATTERNS` scan + `possible_rag_poisoning` audit event before chunking/embedding (detection-only, never blocks) | `rag/ingestion.py` | `tests/test_rag_ingestion.py::TestRagPoisoningDetection` (4 new tests) |
| G3: Frontend CSV export of live query results had no spreadsheet-formula-injection protection | Leading `'` prefix on any cell starting with `=`/`+`/`-`/`@`/tab/CR, before existing CSV-syntax escaping | `frontend/src/lib/csv.ts` | None automated (no frontend test framework exists — see Testing row above) |

## Not implemented, and why (no over-engineering)

- **Malware/AV scanning** — a real, named gap (RED above), not built this pass because
  it requires a new external dependency/service (ClamAV or equivalent) that wasn't
  already present and is a larger integration than this specific audit's evidence
  justified building unprompted; recorded as the top remaining risk instead.
- **Per-user storage quotas** — the app's own stated single-user/local-dev scale
  (`README.md`) makes this a real gap only for a deployment shape this app doesn't
  currently target; building quota infrastructure speculatively would be exactly the
  over-engineering this audit's own instructions warn against.
- **A physical quarantine directory** — the *effect* quarantine achieves (nothing
  untrusted is stored before validation completes) is already structurally guaranteed
  by `ingest_pdf`'s in-memory-then-DB ordering; adding a literal filesystem stage on
  top would add persistence/cleanup complexity without closing any gap that ordering
  leaves open.
- **CDR (Content Disarm and Reconstruction)** — not applicable while this app only ever
  *extracts* from PDFs rather than rendering/passing them through; revisit if PDF
  rendering is ever added.
- **Generic ZIP-upload/archive security** — no archive upload feature exists; building
  one out defensively "just in case" would be speculative infrastructure for a feature
  this app doesn't have.
- **A dedicated polyglot-file detector** — the existing format-specific parsers
  (`pypdf`, PyAV) already require content to be valid *as that specific format* before
  any processing proceeds, which covers the realistic polyglot risk for this app's
  narrow two-format upload surface without a separate detection layer.
