# File Upload / File Processing Threat Model

Companion to `docs/FILE_UPLOAD_SECURITY.md` (controls) and
`docs/FILE_UPLOAD_SECURITY_REPORT.md` (scored findings). Scoped to the file-handling
surface only — see `docs/THREAT_MODEL.md` for the full-application STRIDE model.

## Malicious user

**Threat**: an authenticated-but-hostile user (this app has no per-user tenant
isolation — any authenticated caller has whatever their role grants) uploads a PDF
crafted to exhaust resources, evade moderation, or poison retrieval.

**Mitigations**: byte-size cap (25MB) + page-count cap (2000) before any expensive
extraction; magic-byte check rejects non-PDF content outright; mandatory moderation
gate (Azure Content Safety + text blocklist) blocks the whole asset on any chunk
violation; extracted text is now normalized + injection-pattern-scanned
(`possible_rag_poisoning` audit event) before embedding. **Residual risk**: a
moderation-passing PDF whose text is subtly manipulated to bias retrieval (not flagged
as policy-violating, not pattern-matched) could still influence which chunks retrieve
for a given question — bounded by `rag/graph.py`'s untrusted-data prompt framing, not
eliminated.

## Malicious document

**Threat**: the PDF/audio content itself is the attack vector (not the uploader's
intent necessarily) — e.g. a PDF containing embedded JavaScript, a launch action, or a
malformed object stream targeting a parser vulnerability.

**Mitigations**: this app never *renders* or *executes* PDF content — `pypdf`/`pymupdf`
only extract text and rasterize pages to images, neither of which interprets
JavaScript/launch-actions/annotations. A parser exception (malformed PDF) is caught by
`ingest_pdf`'s broad exception handler and surfaces as a clean "failed" status, not a
crash. **Residual risk**: a genuine memory-safety vulnerability in `pypdf`/`pymupdf`
itself (a supply-chain/dependency risk, not an app-logic gap) is out of this app's
direct control — mitigated only by keeping those dependencies patched (see
`docs/DEPENDENCY_SECURITY.md`).

## Compromised document / provenance

**Threat**: a legitimately-uploaded document is later found to contain something that
should not have been ingested (a false-negative moderation result, or content that
becomes policy-violating after a taxonomy update).

**Mitigations**: every ingested asset's moderation decision (pass/reject, triggering
categories, timestamp) is durably recorded (`moderation.media_assets`), independent of
whether the underlying file bytes are still retrievable — providing an audit trail for
after-the-fact review. A document can be deleted via `DELETE /documents/{id}`
(`DOCUMENTS_WRITE`/delete permission). **Residual risk**: no automated re-scan of
already-ingested content against an updated moderation policy exists — a manual
operator action is required.

## Parser vulnerability

**Threat**: `pypdf`, `pymupdf`, `pytesseract`, `faster-whisper`/PyAV, OpenCV, or
`sentence-transformers`/CLIP have or develop a memory-safety or logic vulnerability
reachable via crafted input.

**Mitigations**: `docs/DEPENDENCY_SECURITY.md`'s pinned-version + `pip-audit` scanning
covers this at the supply-chain level. Every parser call in the ingestion paths is
wrapped in exception handling that fails the *specific file*, not the process (e.g.
`_ocr_suspect_pages`/`_extract_embedded_images`'s per-page/per-image try/except). No
parser runs with elevated privileges or outside the normal Python process sandbox.
**Residual risk**: a zero-day in one of these libraries is not something application-
level code can close — patch cadence is the only real mitigation.

## Storage attack

**Threat**: an attacker attempts to read/overwrite files outside the intended storage
boundary via a crafted filename or stored path.

**Mitigations**: no code path constructs an on-disk path from user input (§15 of the
security doc); `api/media_library.py::_resolve_safe_path` explicitly re-validates path
containment against the configured root before serving; uploaded PDF bytes are stored
in a database column, not the filesystem, eliminating this class of risk for the
primary upload path entirely.

## RAG poisoning

**Threat**: ingested document content is crafted to manipulate the LLM's behavior when
retrieved into a generation prompt (prompt injection via document content), or to bias
which chunks retrieve for unrelated questions.

**Mitigations**: `rag/graph.py`'s generation system prompt explicitly frames retrieved
chunk text as untrusted data, never instructions (the same "data, not instructions"
principle applied to web search results and sampled database values). This pass added
the detection layer this path was missing relative to its siblings: normalized text +
injection-pattern scan + `possible_rag_poisoning` audit logging before embedding
(finding G2). A sensitivity-tagged chunk is never summarized into an answer at all
(fail-closed, since this app has no per-user document authorization system).
**Residual risk**: the injection-pattern list is a curated regex set, not exhaustive —
a sufficiently novel phrasing could evade detection while the structural prompt-framing
defense still bounds the consequence.

## Malware

**Threat**: an uploaded PDF or audio file contains an embedded executable payload
(e.g. inside a PDF's embedded-file stream) intended for a downstream system that later
opens the raw bytes.

**Mitigations**: none specific to malware signature detection — see
`docs/FILE_UPLOAD_SECURITY.md` §19 for why this is a disclosed, accepted gap rather
than a silent one. The moderation gate does not scan for malware, only policy-violating
content. Partial mitigation: the only path where raw original bytes are ever served
back out is `GET /documents/{id}/download` (PDF), which requires authentication +
`DOCUMENTS_READ`(+`_SENSITIVE`) — an attacker would need valid credentials to retrieve
the original bytes, and the download is served as `application/pdf` with
`X-Content-Type-Options: nosniff`, never executed by this app itself.

## Denial of Service

**Threat**: an attacker uploads many large/expensive files rapidly to exhaust CPU,
memory, or storage.

**Mitigations**: `enforce_api_action_rate_limit` bounds `document_upload`/
`voice_transcribe` request rates per client IP; every size/duration/page-count cap in
`docs/FILE_UPLOAD_SECURITY.md` §7 bounds per-request cost; content-hash dedupe
short-circuits re-processing of previously-seen content (both a performance win and an
implicit anti-repeat-upload-abuse measure, since resubmitting the same file costs
almost nothing the second time). **Residual risk**: no per-user/per-IP *storage quota*
exists (an attacker with a valid, unique file per request could still accumulate
significant DB storage over many requests) — acceptable for this app's stated
single-user/local-dev-oriented scale (`README.md`'s Limitations section), a real gap
for a genuine multi-tenant deployment.

## SSRF

**Threat**: file-processing logic is tricked into making a server-side request to an
internal/private address.

**Mitigations**: the only URL-fetch in the file-handling surface is
`media_gen/download.py`'s provider-generated-media download, already SSRF-hardened
(HTTPS-only, resolved-IP checked against private/loopback/reserved/CGNAT ranges — see
that module's own docstring for the resolve-then-connect caveat). No upload path
accepts or follows a URL from user-supplied content (a PDF's embedded external links
are extracted as inert text, never dereferenced).

## Path traversal

Covered in depth above ("Storage attack") — see `docs/FILE_UPLOAD_SECURITY.md` §15-16.

## Privilege escalation

**Threat**: a lower-privileged user uses the upload/download surface to gain access to
content or actions their role shouldn't permit.

**Mitigations**: RBAC gates every upload/download route (`docs/AUTHORIZATION.md`);
sensitivity-tagged document downloads require a distinct, additional permission beyond
ordinary document read access, closing a real prior gap (any `DOCUMENTS_READ` caller
could previously download a sensitivity-tagged PDF) — verified by
`tests/test_api_documents.py`'s sensitivity-gate tests.

## Data leakage

**Threat**: uploaded/processed file content leaks through logs, error messages, or an
unintended response.

**Mitigations**: `docs/OBSERVABILITY.md`'s "what's never logged" section — result
shape only (never content), moderation decisions record category names/counts only.
Rejected content is never persisted beyond its hash. Error messages returned to callers
are generic (`docs/FILE_UPLOAD_SECURITY.md`'s "Incident handling" section) — no parser
internals, file paths, or raw exception text reach the response body (the same
centralized-exception-handling discipline `CLAUDE.md` documents for the rest of the
API). **Residual risk**: extracted PDF text is not sanitized for PII before storage/
embedding — see `docs/OBSERVABILITY.md` §4's equivalent disclosed limitation for typed
questions, which applies identically here: a document containing incidental PII will
have that PII stored in `rag.chunks` and potentially surfaced in a citation, since this
app has no PII-detection/redaction layer for ingested content.
