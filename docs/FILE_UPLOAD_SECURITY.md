# File Upload / File Processing Security

Evidence-based description of how this application handles untrusted file content,
written after a full Phase 0 inventory of every entry point
(`docs/FILE_UPLOAD_SECURITY_INVENTORY.md`) and a section-by-section audit against
OWASP-style file-handling guidance (`docs/FILE_UPLOAD_SECURITY_REPORT.md` has the
scored version of this same material). This app accepts exactly **two** kinds of
user-uploaded file content — PDFs (for RAG) and recorded audio (for voice mode) — plus
one internal, non-upload path (downloading provider-generated media). Everything else
in the checklist this document is modeled on (ZIP/archive uploads, Office documents,
SVG, generic file uploads) does not apply to this app and is stated as such rather than
built speculatively.

## 1. Supported file types

| Feature | Accepted type(s) | Enforced by |
|---|---|---|
| Document/policy RAG (`POST /documents`) | PDF only | Magic-byte check (`%PDF-` prefix), `api/documents.py:118-122` |
| Voice mode (`POST /voice/transcribe`) | Any container `faster-whisper`'s PyAV dependency can decode (typically browser-recorded webm/opus) | Decode-failure handling in `voice/stt.py` (no magic-byte allowlist — see §15 gap) |
| Media library (CLI-only, `scripts/build_media_index.py`) | JPEG/PNG/GIF/BMP/WEBP images; AVI/MP4-family/WebM/Matroska video | Magic-byte signature allowlist, `media/ingest.py:64-86` |
| Generated media download | image/video/audio (provider-declared) | Content-Type allowlist against `image/`/`video/`/`audio/` prefixes, `media_gen/download.py` (2026 Phase 3 fix) |

**Not accepted anywhere**: DOCX/XLSX/PPTX, ZIP/archives, SVG, executables, scripts, or
any generic/unrestricted file type. No endpoint exists to upload them.

## 2. Extension allowlist

Extensions are **never trusted** for type determination in this app — every accepted
format is validated by content (magic bytes / decode success), not filename suffix.
`api/documents.py` doesn't even read the uploaded filename to decide acceptance; only
the byte content is checked. This sidesteps the entire class of double-extension /
encoded-extension / null-byte-extension attacks the checklist's §2 describes, since
extension is not part of the trust decision at all.

## 3. MIME allowlist

The client-supplied `Content-Type` header on an upload is never trusted for content
decisions either. `POST /documents` ignores it entirely in favor of the `%PDF-` magic
bytes. The one place a MIME/content-type value *is* trusted from an external source is
`media_gen/download.py`'s provider response — closed in this pass with an explicit
allowlist (`image/`/`video/`/`audio/` prefixes only; anything else falls back to
`application/octet-stream`, see the security report's finding G1).

## 4. Magic-byte validation

| Path | Check |
|---|---|
| PDF upload | `file_bytes.startswith(b"%PDF-")`, `api/documents.py:118-122` |
| Media-library ingestion | Explicit signature table (JPEG/PNG/GIF87a/GIF89a/BMP, RIFF/WEBP, RIFF/AVI, `ftyp` MP4-family, EBML WebM/Matroska), `media/ingest.py:64-86` |
| Voice audio | No magic-byte check — relies on PyAV's own decode-or-fail behavior (`voice/stt.py`'s `_probe_duration_seconds`), which rejects anything it can't parse as an audio/video container before the expensive transcription step runs. Functionally equivalent (unparseable content never reaches Whisper), but not a pre-decode signature check the way the other two paths have. |

## 5. Content/structure validation

- **PDF**: parsed by `pypdf` (page text) and `pymupdf` (rasterization for OCR) —
  established, actively-maintained libraries, not a hand-rolled parser. Page count is
  capped (`Settings.max_document_pages`, default 2000) *before* any page's text is
  extracted, closing a decompression-bomb-shaped gap the byte-size cap alone leaves
  open (a PDF's on-disk size says little about how many pages it declares).
- **Images**: validated by successful decode through Pillow (tiling path,
  `media/ingest.py`) or OpenCV (video frame reads) — a malformed image fails at decode
  time, not silently.
- **Video**: metadata probed via OpenCV (`cv2.VideoCapture`) before any expensive
  processing (scene detection, transcription) runs.
- **Audio**: PyAV container/codec decode is the validation — unparseable audio raises
  cleanly (`VoiceProcessingError`) rather than being processed further.
- **No CSV/XLSX/DOCX ingestion exists** — that entire content-validation category is
  not applicable.

## 6. Polyglot detection

No dedicated polyglot-file detection exists, and it was judged unnecessary for this
app's actual attack surface: the two upload paths (PDF, audio) each go through a real
format-specific parser (`pypdf`/`pymupdf`, PyAV) that must successfully interpret the
content *as that format* before anything downstream happens — a file that's
simultaneously valid as two formats still only reaches this app's PDF/audio-specific
processing if the PDF/audio parser itself accepts it, which is the same protection a
dedicated polyglot check would add for a two-format allowlist this narrow.

## 7. Maximum file sizes

| Path | Limit | Setting | Enforcement style |
|---|---|---|---|
| PDF upload | 25 MB | `max_document_upload_mb` | Bounded read (`file.read(max_bytes+1)`, 413 if exceeded) — never buffers past the limit |
| PDF page count | 2000 pages | `max_document_pages` | Checked before extracting any page's text |
| Voice audio upload | 10 MB | `voice_max_upload_mb` | Bounded read, 413 if exceeded |
| Voice audio duration | 30 s | `voice_max_duration_seconds` | Probed via PyAV *before* the expensive Whisper call |
| Media-library file | 200 MB | `media_max_file_mb` | `path.stat().st_size` check before processing |
| Generated-media download | 200 MB | `media_max_file_mb` (shared) | Declared `Content-Length` pre-check + streamed byte-count enforcement, `media_gen/download.py` |
| Moderation API text chunk | 10,000 chars | `moderation/provider.py::_MAX_TEXT_LENGTH` | Truncated before the provider call |
| Moderation API image chunk | 4 MB | `moderation/provider.py::_MAX_IMAGE_BYTES` | Truncated (raw byte slice) before base64 encoding |

No request is ever fully buffered into memory before its size limit is checked —
every path above uses a bounded read or a streamed check.

## 8. Archive security

**Not applicable.** No ZIP/DOCX/XLSX/PPTX upload or parsing exists anywhere in this
codebase (confirmed by a repo-wide grep for `zipfile`/`ZipFile` — zero matches). PDF is
the only archive-adjacent format, and its internal compressed-stream handling is
`pypdf`/`pymupdf`'s own, bounded by the page-count and byte-size caps above rather than
a ZIP-specific control (there is no ZIP container to inspect).

## 9. Decompression-bomb protection

Addressed at the format-appropriate layer, not via a generic archive check:
- **PDF**: page-count cap before extraction (§7).
- **Images**: `media_image_tile_threshold_px` (default 2048px) tiles an oversized image
  for moderation rather than processing it as one huge decode; `voice_max_duration_seconds`
  bounds audio decode cost.
- **No ZIP bomb surface exists** since no ZIP container is ever parsed by this app.

## 10-11. Image security / SVG

Image dimension/pixel-flood protection: the tiling threshold (§9) is the primary
control for oversized images before moderation. **SVG is not in the accepted-format
list for any image path** (the magic-byte allowlist is JPEG/PNG/GIF/BMP/WEBP) — SVG
uploads are rejected implicitly by not matching any accepted signature, which is the
recommended posture for a format this app has no functional need for (no SVG sanitizer
exists or is needed).

## 12. PDF security

JavaScript/embedded-action execution is never a risk here because this app never
*renders* or *executes* a PDF — it only extracts text (`pypdf`) and rasterizes pages to
images for OCR (`pymupdf`), neither of which interprets PDF actions, JavaScript, or
launch directives. External links inside PDF text are extracted as plain text (never
auto-followed). The one real resource-exhaustion control is the page-count cap (§7).

## 13. Office document security

**Not applicable** — no DOCX/XLSX/PPTX ingestion path exists.

## 14. CSV formula injection

**Backend**: no CSV/XLSX export of user data exists in the API layer. **Frontend**: a
real gap was found and fixed this pass — `frontend/src/lib/csv.ts`'s CSV export of live
query results (`ResultsTable.tsx`) had no spreadsheet-formula-injection protection. A
result cell beginning with `=`/`+`/`-`/`@` is now prefixed with `'` (the standard OWASP
mitigation) before CSV-syntax escaping, neutralizing the formula trigger without
changing what the user sees when they open the file.

## 15. Filename security

No code path anywhere builds an on-disk file path from a user-supplied filename string
(confirmed in the Phase 0 inventory). Every temp file is either fully random
(`tempfile.mkstemp`) or content-hash-derived. The one filename-adjacent data flow — an
embedded PDF image's internal filename suffix used as `mkstemp`'s `suffix=` argument
(`rag/ingestion.py`) — never becomes a path component, only a fixed-position string
suffix on an OS-generated random name, so path-traversal/reserved-name/null-byte
concerns don't apply. Original filenames (PDF upload name, etc.) are stored only as
metadata, never as a storage path.

## 16. Path traversal

Every serve-back route resolves an **opaque id** through a database/metadata lookup
first — none accept a raw filesystem path from the client. Where a path *is* read back
from stored metadata (media-library serving, since Chroma stores a `source_path`),
`api/media_library.py::_resolve_safe_path` explicitly re-validates
`Path(raw_path).resolve().is_relative_to(allowed_root.resolve())` before ever opening
the file — defense-in-depth against a stale or manipulated stored path, mirroring the
SSRF-hardening pattern used for provider URL downloads.

## 17. Storage isolation

Uploaded PDF bytes live in SQL Server (`rag.documents.pdf_bytes`), not on the
application filesystem at all. Generated media lives only in an in-process, bounded
memory cache — never disk, never a web-servable directory. The one filesystem writes
this app makes for content it processes are: OS temp directory (`tempfile.mkstemp`,
auto-cleaned in `finally` blocks) and `media/.thumbnails/` (a dedicated, non-web-root
directory, content-hash-named files, path-containment-checked on read per §16). None of
these overlap with source code, the web root (`frontend/dist`), or configuration
directories.

## 18. Quarantine model

An explicit multi-stage quarantine workflow (upload → quarantine dir → validate →
scan → sanitize → move to trusted storage) was **not built** as a separate physical
stage, and this is a deliberate scoping decision, not an oversight: the *effect* of a
quarantine model — untrusted content is never trusted/stored/embedded until validation
completes — is already achieved by this app's existing ordering guarantee.
`rag/ingestion.py::ingest_pdf` performs extraction → moderation *entirely in memory/temp
files* and only calls `rag/store.py::insert_document` (which persists bytes) after
`decision.passed` is confirmed true; a rejected file never gains a database row at all.
The database row itself *is* the "trusted storage" boundary here — there is no
intermediate on-disk quarantine directory because there is no on-disk storage of
accepted content in the first place (bytes go straight from upload buffer to
extraction/moderation to DB). Building a literal quarantine directory on top of this
would add a persistence layer and cleanup responsibility for no additional safety this
in-memory ordering doesn't already provide.

## 19. Malware / antivirus scanning

**Not implemented — an accepted, disclosed gap, not an oversight.** This app's content
inspection (`moderation/gate.py`, Azure AI Content Safety) checks for policy-violating
*content* (hate/violence/sexual/self-harm categories, plus a text blocklist for
weapons/drugs) — it does **not** perform signature-based malware/virus scanning of the
uploaded bytes themselves. Given the accepted format set (PDF text/images, audio) is
processed exclusively through Python library parsers that never execute embedded
content (§12), and no format capable of carrying a classically-executable payload is
ever accepted, the risk this control would close is narrow for this app's specific
threat model — but it is a real, named gap worth closing before handling uploads from a
genuinely adversarial multi-tenant population (e.g. ClamAV integration ahead of the
moderation gate). See `docs/FILE_UPLOAD_SECURITY_REPORT.md`'s scoring for this
category.

## 20. Content Disarm and Reconstruction (CDR)

**Not implemented, not recommended for this app's current scope.** CDR's value is
highest for formats that carry active/executable content this app might otherwise pass
through unmodified (macros, embedded OLE objects, JavaScript-bearing PDFs) — but this
app never passes an uploaded file through unmodified in the first place: PDF content is
always *extracted* (text/rasterized images only, never the original PDF structure) for
everything downstream of ingestion except the optional raw-bytes-for-download feature
(`enable_pdf_download`), where the original bytes are only ever served back as
`application/pdf` for the uploader to view in their own PDF reader, never re-parsed by
this app. If PDF *rendering* (not just extraction) is ever added, CDR should be
revisited.

## RAG security

Covered in depth by `CLAUDE.md`'s "Document/policy agentic RAG" section (untrusted-data
prompt framing, sensitivity-category fail-closed generation) and extended this pass:
extracted PDF text is now normalized (`security.sanitization.normalize_text`, closing
the homoglyph-obfuscation gap) and scanned for injection-shaped patterns before
chunking/embedding, logging a `possible_rag_poisoning` audit event on a match
(detection-only, never blocking — the moderation gate and the prompt-framing defense
remain the real backstops) — see the security report's finding G2 for why this closes
a real asymmetry with this codebase's other untrusted-text entry points.

## Download security

See §16-17 above and `docs/FILE_UPLOAD_SECURITY_INVENTORY.md`'s "Serve-back routes"
table. Every download route now sets `X-Content-Type-Options: nosniff`
(2026 Phase 3 addition, all three serve-back routes) as defense-in-depth against
browser MIME-sniffing, on top of the Content-Type allowlist fix (finding G1).

## Authentication / authorization

Every upload/download route requires authentication and a specific RBAC permission
(`DOCUMENTS_WRITE`/`DOCUMENTS_READ`/`DOCUMENTS_READ_SENSITIVE`/`VOICE_USE`/
`MEDIA_GENERATE`/`MEDIA_SEARCH` — see `docs/AUTHORIZATION.md`), verified end-to-end by
`tests/test_api_authz.py`. Sensitivity-tagged documents require an additional
permission beyond ordinary read access (`api/documents.py:212-227`).

## Logging

Moderation decisions, RAG-poisoning-scan matches, and every upload's rate-limit/size
outcome are logged via `security.audit_log.log_security_event` — see
`docs/OBSERVABILITY.md`'s event taxonomy table. **Never logged**: raw file bytes, full
extracted document text, or moderation chunk content (only category names/counts) —
consistent with `CLAUDE.md`'s "never log full result rows" discipline applied to files.

## Incident handling

A rejected upload returns a generic, non-technical error (`"This file was rejected by
content moderation and was not ingested."`) — the specific triggering category is never
returned to the caller, only recorded server-side (`moderation.media_assets`). A
moderation-provider outage aborts ingestion with a clean error rather than silently
treating the failure as a pass (`moderation/provider.py` deliberately does not catch
`httpx.HTTPError`).

## Configuration

Every size/count/duration limit referenced above is a `Settings` field
(`config/settings.py`), sourced from `.env`, with a safe, non-unlimited production
default — no hardcoded literal anywhere in the processing code. See
`docs/FILE_UPLOAD_SECURITY_REPORT.md`'s configuration table for the complete list.

## Security test matrix

See `tests/test_rag_ingestion.py` (moderation-gate ordering, dedupe, RAG-poisoning
detection), `tests/test_media_ingest.py` (media-library validation/moderation),
`tests/test_media_gen.py` (SSRF/size/content-type allowlist on downloads),
`tests/test_api_documents.py`/`tests/test_api_voice.py` (upload endpoint size/auth/rate
limits), `tests/test_api_media_library.py` (path-containment). Full list and any gaps
in `docs/FILE_UPLOAD_SECURITY_REPORT.md`'s test-coverage section.

## Remaining risks

See `docs/FILE_UPLOAD_SECURITY_REPORT.md`'s "Remaining risks" section for the complete,
severity-ranked list — no malware/AV scanning (§19) is the most significant one.
