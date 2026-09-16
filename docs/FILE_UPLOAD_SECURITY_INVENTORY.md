# File Upload / File Processing Security — Phase 0 Inventory

Every place a file or file-like content enters or is processed by this application,
found by direct code inspection (not inferred from names). This is the input to the
gap analysis in `docs/FILE_UPLOAD_SECURITY_REPORT.md` — read that document for the
scored audit; this one is the evidence trail.

## Entry points

| # | Endpoint / trigger | Content | Source | Existing validation |
|---|---|---|---|---|
| 1 | `POST /documents` (`api/documents.py:87-146`) | PDF only | HTTP multipart (`UploadFile`) | AuthZ (`DOCUMENTS_WRITE`) → feature flag → rate limit (`document_upload`) → **bounded read** (`file.read(max_bytes+1)`, 413 over `max_document_upload_mb`=25MB) → **magic-byte check** (`%PDF-` prefix, 400 otherwise) → delegated to `rag.ingestion.ingest_pdf` |
| 2 | `rag/ingestion.py::ingest_pdf` (called only from #1) | PDF pages, embedded images, OCR'd scanned pages | The uploaded bytes | Content-hash dedupe (short-circuits re-processing) → **page-count cap** (`max_document_pages`=2000, `ValueError` past it) → OCR rasterization via `pymupdf` for suspect pages (`_OCR_SUSPECT_CHAR_THRESHOLD`) → embedded-image extraction via `pypdf` → **mandatory moderation gate** (hard-reject blocks storage entirely, no partial ingestion) → chunk → embed → store |
| 3 | `media/ingest.py::ingest_file` | Image/video | **CLI-only** — `scripts/build_media_index.py` walks an operator-configured local directory (`Settings.media_library_path`). **Not a user-facing HTTP upload path.** | Size cap (`media_max_file_mb`=200MB) → **magic-byte sniff** (never extension) against an explicit signature allowlist (JPEG/PNG/GIF/BMP/WEBP/AVI/MP4-family/WebM) → content-hash dedupe → image tiling for moderation (`media_image_tile_threshold_px`) or scene-segmented video moderation → mandatory moderation gate (asset-level: one rejected segment blocks the whole video) |
| 4 | `media_gen/download.py::download_media_bytes` | Provider-generated image/video/audio | A URL returned by the IMA Studio API (not a user upload — internal pipeline step, `agent/orchestrator/nodes.py::execute_generation`) | **SSRF hardening**: HTTPS-only, resolved-IP checked against private/loopback/reserved/CGNAT ranges → size cap (both declared `Content-Length` pre-check and streamed byte-count enforcement) → **no moderation, no content-type sniffing** (see gap G1 below) |
| 5 | `moderation/provider.py` (`_azure_content_safety`) | Text (≤10,000 chars, truncated) / image (≤4MB, truncated) chunks from #2/#3 | Internal — never receives raw asset bytes beyond the truncated slice | Fails closed on missing config; a provider outage is NOT caught (aborts ingestion rather than treated as a pass) |
| 6 | `POST /voice/transcribe` (`api/voice.py:42-79`) | Audio (any container `faster-whisper`'s PyAV can decode) | HTTP multipart (`UploadFile`) | AuthZ (`VOICE_USE`) → feature flag → rate limit (`voice_transcribe`) → bounded read (`voice_max_upload_mb`=10MB, 413 over) → **duration probe before the expensive Whisper call** (`voice_max_duration_seconds`=30s via PyAV, 413 over) → decoded in-memory only, never written to disk, **never persisted anywhere** |
| 7 | (cross-cutting) All `UploadFile`/multipart usage | — | — | Exactly two routes in the entire codebase: #1 and #6. No generic/other file-upload endpoint exists. |
| 8 | (cross-cutting) `subprocess`/`os.system`/`shell=True` | — | — | **Zero matches anywhere in the repository.** Every "external tool" (Tesseract, PyMuPDF, OpenCV/PySceneDetect, faster-whisper/PyAV) is a Python library call, not a subprocess/shell invocation — no command-injection surface exists. |
| 9 | (cross-cutting) Disk writes with user-input-derived names | — | — | Every temp file is either `tempfile.mkstemp`-randomized (OCR rasterization, embedded-image extraction, image tiling) or content-hash-derived (video keyframe thumbnails). One case (`rag/ingestion.py:207`) takes a `.suffix` (e.g. `.jpg`) from an embedded image's internal PDF filename, but only as the `mkstemp` suffix argument, never as a path component — no traversal risk. **No code path builds an on-disk path from a raw user-supplied filename string.** |
| 10 | (cross-cutting) ZIP/archive handling | — | — | **No `zipfile` usage anywhere in the repository. No DOCX/XLSX ingestion path exists.** PDF is the only archive-adjacent format accepted, parsed by `pypdf`/`pymupdf` (their own internal decompression), bounded by the page-count and byte-size caps above, not a ZIP-bomb-specific control. |
| 11 | (cross-cutting) Storage destinations | — | — | See table below. |
| 12 | (cross-cutting) Serve-back routes | — | — | See table below. |

## Storage destinations

| Content | Store | Gate |
|---|---|---|
| Original PDF bytes | `rag.documents.pdf_bytes` (SQL Server `VARBINARY(MAX)`) | `Settings.enable_pdf_download`; only written after moderation passes |
| PDF chunk text + embedding | `rag.chunks` | After moderation passes |
| Moderation decision metadata | `moderation.media_assets` (hash, status, **category names/counts only, never content**) | Always recorded, pass or reject |
| Ingested image/video embeddings | Chroma `media_images` / `media_video_segments` | After moderation passes |
| Video keyframe thumbnails | Local disk, `media/.thumbnails/`, content-hash-named | After moderation passes |
| Generated (IMA) media bytes | **In-process memory only** (`MediaCache`, FIFO-bounded to 100 entries) | Never disk, never DB, no moderation |
| Recorded voice audio | **Never persisted** — decoded in-memory, discarded after transcription | N/A |

Rejected content (a moderation hard-reject) is never stored beyond its hash/category —
confirmed by `rag/ingestion.py`'s ordering (store call only reached after
`decision.passed`) and `moderation/store.py`'s own contract.

## Serve-back routes

| Route | AuthZ | Path-traversal / content-type handling |
|---|---|---|
| `GET /documents/{id}/download` | `DOCUMENTS_READ` + `DOCUMENTS_READ_SENSITIVE` for tagged docs | Fixed `media_type="application/pdf"`; resolved through a DB id lookup, never a raw path |
| `GET /media/{media_id}` (generated media) | `MEDIA_GENERATE` | **Content-Type trusted verbatim from the IMA provider's own response header at download time, no re-validation against the actual bytes** (see gap G1) |
| `GET /media/library/{media_id}` (media-search library) | `MEDIA_SEARCH` | `_resolve_safe_path`: `Path(raw_path).resolve().is_relative_to(allowed_root)` before ever opening the file — explicit path-containment defense-in-depth against a stale/manipulated Chroma-stored path. Content-Type from a fixed extension→MIME table, never from an untrusted source. |

No generic "download by arbitrary path" endpoint exists anywhere — every serve-back
route resolves an opaque id through metadata/DB lookup first.

## Negative findings (scope-narrowing — confirmed absent, not overlooked)

1. No SVG handling anywhere (image allowlist is JPEG/PNG/GIF/BMP/WEBP only) — §11 of the
   audit checklist (SVG risk) is out of scope by construction.
2. No ZIP-upload feature, no `zipfile` usage, no DOCX/XLSX/PPTX parsing anywhere.
3. No generic file-download-by-path endpoint.
4. No `subprocess`/`os.system`/`shell=True` anywhere — no command-injection surface.
5. `media/ingest.py`'s full pipeline (PySceneDetect, image tiling, keyframe extraction)
   is CLI-only, never reachable from an HTTP request.
6. No filename-derived disk paths.
7. No CSV/XLSX data export **on the backend** — see gap G3 below for a real one found
   on the **frontend**.

## Gaps identified (carried into the scored report)

- **G1**: `GET /media/{media_id}` reflects the IMA provider's own `Content-Type` header
  verbatim with no allowlist — a compromised/buggy provider response could set an
  unexpected content type (e.g. `text/html`) served back to the browser under this
  app's own origin.
- **G2**: RAG-ingested PDF text (extracted page text, OCR'd text) is never run through
  `security/sanitization.py`'s `normalize_text` + injection-pattern-scan +
  `log_security_event` — the exact defense-in-depth pattern already applied to
  retrieved schema content, golden examples, and `conversation_history`
  (`agent/nodes.py`'s `possible_rag_poisoning` event). RAG chunk content only has the
  structural "untrusted data" prompt-framing defense in `rag/graph.py`, not the
  detection/audit-logging layer its sibling ingestion paths already have.
- **G3**: `frontend/src/lib/csv.ts`'s CSV export of real query result rows
  (`ResultsTable.tsx`) had no spreadsheet-formula-injection protection — a result cell
  beginning with `=`/`+`/`-`/`@` would execute as a formula when the downloaded CSV is
  opened in Excel/Sheets/LibreOffice. **Fixed in this pass** (see the scored report).
