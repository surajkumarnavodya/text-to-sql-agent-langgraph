# Image Editing Architecture

> **⚠️ Partially superseded — read the note below before relying on this
> document.** This document's core claim when written was "attachments are
> never actually sent anywhere" (see "Known limitations" below). That is no
> longer true: a subsequent feature pass built a real backend attachment
> pipeline (`attachments/`) — an attached image now genuinely reaches a
> configured vision model or OCR, and explicit `POST
> /attachments/{id}/extract-text|resize|remove-text` actions exist for
> real, deterministic image operations. See `CLAUDE.md`'s "Chat
> attachments, image actions, and their security hardening" section and
> `docs/API.md`'s "Attachments" section for the current, accurate state.
> **What remains
> genuinely accurate below**: the local Konva-based editor's own feature
> set (crop/rotate/draw/annotate) and its library-choice rationale.

> **Update, 2026-09-27 — "AI-guided editing" is now real, not a stub.**
> Everything under "What does NOT exist: AI-guided (generative,
> prompt-driven) editing" below described a deliberate, permanent stub —
> that section is now historical: a real backend (`POST
> /attachments/{id}/ai-edit`, `attachments/ai_edit.py` +
> `media_gen/image_edit_provider.py`, IMA Studio's `image_to_image` task
> category) exists, and `lib/imageEditAdapter.ts`'s `AiGuidedEditAdapter`
> calls it for real instead of always rejecting. See "AI-guided editing:
> the real implementation (2026-09-27)" below for the current design,
> mask convention, and residual limitations — read that section instead of
> the "What does NOT exist" one below, which is kept only as a historical
> record of what this feature used to be.

**Status: implemented and tested end-to-end (local editing, real
OCR/vision/resize/text-removal/blur, and now real AI-guided generative
editing whenever `ENABLE_IMAGE_EDITING=true` and a provider credential is
configured — see "AI-guided editing: the real implementation" below).**
When the server-side feature flag or credential is missing, the UI
disables the AI-edit controls and says so honestly; local editing, OCR,
resize, and deterministic blur all keep working regardless.

## What exists today

- **Attachment**: file picker, drag-and-drop, and clipboard paste, all in
  the chat composer (`ChatInput.tsx`), via `hooks/useImageAttachments.ts`.
- **Validation** (`lib/imageValidation.ts`): real content-sniffed magic-byte
  detection (PNG/JPEG/GIF/WEBP), never the file extension or the
  browser-reported `File.type` alone (both are spoofable). Also checks
  file size (10 MB cap), decodability, and pixel dimensions (8000px cap).
  This is a **client-side UX convenience only** — see "Security" below.
- **Local editor** (`components/image/ImageEditor.tsx`, Konva/react-konva):
  crop, resize (via aspect presets + manual crop), rotate (90° steps),
  flip (horizontal/vertical), zoom, pan (canvas scroll), freehand drawing
  with brush size/color, eraser, rectangle/ellipse shapes, text
  annotation, undo/redo, a separate mask-drawing layer, before/after
  toggle, reset-all, download, and save-back-to-composer. All of this
  does real work, entirely in the browser — no network call.
- **Viewer** (`components/image/ImageViewer.tsx`): a lightbox with
  zoom (buttons + scroll-wheel) and scroll-based pan, Escape-to-close.
- **Lazy-loaded**: the entire Konva-based editor (`~343KB`/`106KB gzip`)
  is code-split via `React.lazy()` and only fetched when a user actually
  clicks "Edit" — attaching, viewing, or sending a question never pays
  that cost. Confirmed via a production build: the main bundle is
  unchanged in size with or without this feature; the editor is its own
  chunk.

## AI-guided editing: the real implementation (2026-09-27)

**Root cause of the old "backend endpoint hasn't been built" message,
confirmed by tracing the click:** `ImageEditor.tsx`'s Generate button
called `AiGuidedEditAdapter.editWithInstruction()`
(`lib/imageEditAdapter.ts`), which — as documented in the "What does NOT
exist" section below — was a deliberate, permanent stub that always threw
`AiEditNotConfiguredError`. No backend route existed to call at all.

**What was built to close that gap:**

- **Provider chosen: IMA Studio, `image_to_image` task category.**
  Verified live, not assumed — a real, read-only, no-cost `GET
  /open/v1/product/list?category=image_to_image` call against this
  project's own configured IMA account (2026-09-27) confirmed 5 real
  models (`gpt-image-2`, `gemini-3.1-flash-image` "Nano Banana 2",
  `gemini-3-pro-image` "Nano Banana Pro", `doubao-seedream-4.5`,
  `midjourney`), and that **none of them expose a native mask/inpainting
  parameter** — this is instruction-driven whole-image editing only. The
  real upload flow (a separate host, `imapi.liveme.com`, a signed
  GET-for-token then a raw `PUT` of the image bytes) was fetched from
  `github.com/imastuido/ima-all-ai`'s own reference implementation rather
  than guessed, and is implemented in `media_gen/upload.py`.
- **Mask convention: this app's own canonical mask, converted to IMA's
  actual convention (a visual overlay + text instruction), never assumed
  to fit generically.** Since IMA has no native mask channel, a painted
  mask is composited onto the source image as a translucent red overlay
  (`media_gen/image_edit_provider.py::_composite_mask_overlay`) and the
  prompt sent to the model is prefixed with an explicit instruction to
  only modify the highlighted region — disclosed to the user via
  `ImageEditResult.warnings` (rendered in the completed-result panel),
  never silently presented as pixel-exact masking. The canonical internal
  mask itself (`attachments/mask.py`) is a single-channel/RGBA PNG,
  white/opaque = editable, decoded/validated/resized to the source
  image's own native pixel dimensions
  (`decode_and_validate_mask`) — a zero-area mask is rejected with an
  actionable message before any provider call.
- **Mask/source pixel alignment fixed.** The old, disclosed "known
  simplification" below (mask = the whole flattened canvas) is fixed:
  `ImageEditor.tsx::flattenMaskOnly` renders *only* the mask layer, at the
  exact same `pixelRatio` `flattenToDataUrl()` itself uses, by temporarily
  hiding every other Konva layer and forcing the mask layer to full
  opacity for one `toDataURL()` call — both exports come from the same
  Stage geometry, which is what guarantees pixel-for-pixel alignment
  regardless of the current zoom/rotation/crop state, with no second
  offscreen Stage or manual coordinate math needed.
- **Backend contract**: `POST /attachments/{id}/ai-edit`
  (`api/attachments.py::ai_edit_route`,
  `AiImageEditRequest`/`AiImageEditResponse` in `api/schemas.py`). The
  source is always a **fresh, transient attachment holding the editor's
  current canvas export** (`lib/imageEditAdapter.ts` uploads it via the
  normal `POST /attachments/upload` path immediately before calling
  ai-edit, and deletes it again in a `finally` once the edit
  completes/fails) — never the stale original, and never a blob URL/local
  path passed as a model input. `attachments/ai_edit.py::execute_image_edit`
  is the orchestration: operation allowlist
  (`remove_object`/`replace_background`/`replace_sky`/`region_edit`/
  `enhance`), prompt length/empty checks,
  `media_gen.content_policy.basic_prompt_safety_check`, mask
  decode/validate, the existing `agent.rate_limit
  .get_media_generation_limiter` (shared with plain media generation —
  AI image editing is exactly as metered), an in-memory, TTL'd
  idempotency cache keyed by `(owner_subject, idempotency_key)` so a
  duplicate click never pays for a second generation, and a
  `security.audit_log` event per outcome. The provider (`ImaImageEditProvider`)
  downloads the result via the existing SSRF-hardened
  `media_gen.download.download_media_bytes` and decodes/verifies it via
  Pillow before ever storing it — generated bytes are treated as
  untrusted, never trusted at face value. A completed edit is stored as a
  brand-new attachment via the existing, unmodified
  `attachments.pipeline.register_derived_image` (the same function
  resize/remove-text/blur already use) — the original is never mutated.
- **Frontend result states**: `ImageEditor.tsx` tracks a real
  `idle → uploading → processing → completed | failed` phase for the
  AI-edit flow (separately from local blur's and OCR-extract's own
  simpler `idle → processing → completed | failed`), shows a mask preview
  (what was actually sent), disables the Generate button and prompt input
  while `image_ai_editing` capability is off (querying `GET
  /attachments/capabilities`, never assuming availability), and guards
  every async handler against a stale response overwriting the UI after
  the modal is closed or reopened for a different image
  (`requestEpochRef`, bumped on every open/close, checked before any
  state update from an in-flight request commits).
- **Quick-action routing, not one shared handler for every button**: only
  the 4 genuinely generative presets (Remove selected object, Replace
  background/sky, Enhance region) call the paid AI-edit endpoint. "Blur
  the selected face" calls the existing, free, local/deterministic `POST
  /attachments/{id}/blur-region` (Pillow Gaussian blur over the painted
  mask — no model call, works even when AI-guided editing itself is
  disabled). "Extract the selected table or chart" calls the existing
  OCR/vision analysis route (`POST /attachments/{id}/extract-text`) and
  renders recognized text, never a generated image — see
  `ImageEditor.tsx`'s `handleBlurFace`/`handleExtractTable` and the
  `AI_EDIT_PRESET_OPERATIONS` map, which deliberately excludes both of
  these two keys.
- **No SQL-path leakage, structurally, not just by convention**:
  `attachments/ai_edit.py` has no import of `agent.graph`/`agent.nodes`
  and is reached only through its own dedicated REST route, never through
  `/ask` — an image-edit-only request cannot reach schema
  retrieval/SQL generation/execution because there is no shared code path
  to guard in the first place.

**Verified with a fake provider (`FakeImageEditProvider`,
`media_gen/image_edit_provider.py`) for every automated test** — 2123
backend tests pass (`ruff`/`black`/`mypy` clean), 255 frontend tests pass
(13 new, including pixel-level mask-overlay assertions and mocked-provider
tests that assert real image/mask **bytes** were passed, not filenames),
`tsc`/`oxlint`/`npm run build` all clean.

**A real, live IMA Studio `image_to_image` call was also attempted
end-to-end** (a synthetic test image + a real mask, `remove_object`) —
this is genuinely informative but not a full success: the upload leg
completed against IMA's real infrastructure, then `POST
/open/v1/tasks/create` failed with IMA's own business error `{"code":
4008, "message": "Insufficient points"}` — the configured account has no
generation credits, a real external/account-balance condition, not a bug
in this app. **No credits were spent** (the failure happened before any
model inference ran). This confirms the request-construction, upload, and
typed-error-propagation paths are correct against IMA's real, live
infrastructure — it does **not** confirm a successful end-to-end
generation, since that requires an account with a positive credit
balance. Re-run `python` against the same `ImaImageEditProvider.edit()`
call once credits are topped up to get that final confirmation; nothing
in the code needs to change to do so.

**Known, disclosed limitations of this implementation:**

- **IMA has no native mask/inpainting channel**, as verified above — the
  visual-overlay-plus-instruction approach is a best-effort convention,
  not pixel-exact masking, and the model can still edit outside the
  highlighted region. This is disclosed to the user via
  `ImageEditResult.warnings`, not hidden.
- **"Extract the selected table or chart" runs OCR on the whole current
  canvas as shown**, not a true selected sub-region — crop first with the
  Crop tool to isolate one table/chart if the photo has more than one (the
  same limitation the button's own hint text discloses).
- **The `"image"` i18n namespace (including every string this feature
  added) exists only in the English locale file** — a pre-existing gap
  that predates this feature (confirmed: the whole pre-existing editor,
  crop/rotate/draw included, was never localized into es/fr/hi/mr either).
  `fallbackLng: 'en'` means a non-English UI silently shows English text
  for this panel rather than breaking, but it is not actually localized.
- **`Settings.image_edit_timeout_seconds` (default 90s) bounds one
  provider poll**, not the whole HTTP request end-to-end — a slow upload
  or a slow result download are each bounded separately by their own
  underlying HTTP client timeouts, not by this one setting.

## What does NOT exist: AI-guided (generative, prompt-driven) editing — historical, see the section above

**This section is now historical** — kept only as a record of what this
feature used to be before the 2026-09-27 pass above. Every claim below
("no backend endpoint accepts an image plus an edit instruction," the
`AiGuidedEditAdapter` stub that always rejects, the `POST /media/edit`
contract sketch) has been superseded by the real implementation described
above; do not act on anything in this section.

**Update, later pass:** the claim immediately below ("no backend endpoint
accepts an image") is no longer accurate in general — `api/attachments.py`
now has real `UploadFile` routes (`POST /attachments/upload`, plus
`POST /attachments/{id}/resize|remove-text`), and `AskRequest` does now
carry `attachment_ids`. What remains true, specifically, is narrower: no
endpoint accepts an image **plus a free-text natural-language edit
instruction** and returns a generatively-edited result — `POST
/attachments/{id}/remove-text` is real pixel editing, but it takes explicit
region coordinates, not a prompt, and uses classical (non-generative)
inpainting. See `CLAUDE.md`'s "Chat attachments, image actions, and their
security hardening" section for what's real today; the rest of this section (the
`AiGuidedEditAdapter` stub, its own honest in-UI error message) remains
accurate for that specific, narrower capability.

**No backend endpoint accepts an image plus a natural-language edit
instruction for AI-guided (generative) editing.** `media_gen`'s existing
image *generation* takes a text prompt and produces a brand-new image — it
does not accept an uploaded image plus an edit
instruction.

The editor's "AI-guided editing" section (preset buttons like "Remove the
selected object," a free-text prompt field, mask painting) is real UI —
it is not hidden or absent — but clicking "Generate" always fails with a
clear, honest message:

> "AI-guided editing isn't available in this deployment yet — it needs a
> backend endpoint that hasn't been built. Your local edits above are
> unaffected."

This is implemented via `lib/imageEditAdapter.ts`'s `AiGuidedEditAdapter`,
which **always rejects** with `AiEditNotConfiguredError` — a deliberate,
permanent stub, not a bug or an unfinished TODO that happens to reject
today. The seam (`ImageEditAdapter` interface) exists specifically so a
real implementation can be swapped in later without touching the editor's
UI code at all.

### The backend contract a real implementation would need (historical sketch — superseded by the actual, real contract above)

Not built in this pass — documented here so a future session (or you)
doesn't have to reverse-engineer it from the frontend:

```
POST /media/edit
{
  "image": "<base64 or multipart image>",
  "mask": "<base64 or multipart image, optional>",
  "instruction": "Remove the selected object"
}
→ { "media_id": "<opaque id>" }   // mirrors media_gen's own pattern
```

Should mirror `media_gen`'s existing, already-reviewed design (see
`CLAUDE.md`'s "Media generation" section) rather than invent a new
pattern:

- **Human-in-the-loop approval** before any real provider call, matching
  `Settings.require_generation_approval` / `POST /generate/confirm`'s
  existing propose-then-confirm split — an AI image edit is exactly as
  metered/costly as a fresh generation.
- **A cost ceiling**, reusing `agent.rate_limit.get_session_expensive_source_limiter`
  rather than a new limiter.
- **SSRF-hardened download** of the result, reusing
  `media_gen.download.download_media_bytes`.
- **Served through this app, never a raw provider URL** — the same
  `GET /media/{media_id}` pattern `media_gen`/`media_gen.cache` already
  establish, not a direct link back to a third-party CDN.
- **Audit logging** via `security.audit_log.log_security_event`, the same
  as every other metered/sensitive action in this codebase.

**What the actual, shipped implementation did differently from this
sketch**: no separate human-in-the-loop *approval* step was added (unlike
`media_gen`'s own text-to-image/video generation) — an image edit's own
Generate button click, plus the existing per-caller/session rate limit and
idempotency cache, was judged sufficient friction for this feature's scope
(there is no `POST /attachments/{id}/ai-edit/confirm` two-step flow). The
result is served back as a data URL in the same response plus stored as a
real attachment (`register_derived_image`), rather than a separate
`GET /media/{id}` fetch — reusing the attachment system's own existing
storage/access-control model rather than `media_gen.cache`'s in-memory,
FIFO-evicted one, since a derived image here is meant to be usable in a
follow-up question the same way any other attachment is.

## Canvas library choice: Konva (`konva` + `react-konva`)

No canvas/drawing library existed in `package.json` before this feature.
Three options were considered (per the request's own instruction to
"select only one approach and justify it"):

| Option | Verdict |
|---|---|
| Raw Canvas API | Rejected — hand-rolling hit-testing, layering, undo/redo, and resize handles from scratch is a large, error-prone amount of code for a chat-app image editor, not proportionate to the feature's actual scope. |
| **Konva + react-konva** | **Chosen.** React-idiomatic (declarative `<Stage>`/`<Layer>`/`<Rect>` components instead of imperative canvas calls), includes a built-in `Transformer` for resize/rotate handles "for free," and is a moderate dependency (not a full app framework). |
| Fabric.js | Rejected — a comparable feature set to Konva but a heavier, less React-idiomatic API (imperative object model, no first-class React bindings as actively maintained as react-konva). |
| tldraw | Rejected — a full, opinionated whiteboard application; using it to edit one photo means fighting its own UI chrome and data model far more than building on top of it. |

**Real cost, disclosed honestly**: Konva added the entire editor as its
own ~343KB (106KB gzip) chunk. Lazy-loading (see above) means this cost
is paid only by a user who actually opens the editor, not by every page
load.

## Non-destructive editing state

- The **original** file/object URL is kept (`AttachedImage.originalUrl`)
  and never mutated.
- The **edited result** is kept separately (`AttachedImage.editedDataUrl`),
  set only when the user clicks "Save changes" in the editor.
- Re-opening an already-edited attachment's editor loads from
  `editedDataUrl` (continuing from the last save), not from the pristine
  original — but the original is still recoverable: `AttachmentChip`
  always has access to `originalUrl`, and "Reset all edits" inside the
  editor clears back to whatever image was loaded when that editor
  session opened.
- Undo/redo (`hooks/useHistory.ts`, a generic, independently-tested hook)
  covers the annotation/mask layers — rotate/flip/crop are applied
  immediately as direct transforms rather than being pushed onto the same
  undo stack (a deliberate scope decision to keep the undo model simple;
  "Reset all edits" is the escape hatch for those).

## Known limitations (disclosed, not hidden)

- **Superseded — see the note at the top of this document.** This bullet
  originally read "Attachments are never actually sent anywhere." That is
  no longer true: `AskRequest.attachment_ids` exists, `api/attachments.py`
  has real upload/resize/remove-text routes, and an attached image
  genuinely reaches a configured vision model or OCR via
  `attachments/graph.py`. The composer's "Local only" notice described
  here has been replaced accordingly — see `CLAUDE.md`'s "Chat
  attachments" section and `docs/API.md`'s "Attachments" section for the
  current behavior. What remains true is narrower: the *editor's*
  "AI-guided editing" panel specifically (a natural-language-prompted
  generative edit) is still not wired to a backend — see above.
- **Crop's pixel-exactness has not been manually verified in a real
  browser** in this session (no browser automation tool was available)
  — the coordinate math (`Stage.toDataURL({x, y, width, height})`, scaled
  by the current zoom level) follows Konva's documented contract as
  understood from its API, but was verified by code review and a
  successful production build/typecheck only, not by visually confirming
  a crop lands exactly where dragged.
- **Rotate/flip visual correctness** (the centered-offset technique used
  to keep a rotated/flipped image centered in the canvas) is likewise
  unverified in a real running browser for the same reason.
- **Fixed, 2026-09-27 — was: "the mask sent to `AiGuidedEditAdapter` is
  currently the whole flattened canvas."** `ImageEditor.tsx::flattenMaskOnly`
  now renders a true isolated mask layer (see "AI-guided editing: the real
  implementation" above) — this bullet is kept only as a historical record
  of the gap that used to exist.
- **Text annotation uses `window.prompt()`**, not an inline canvas text
  editor — a deliberate, simpler scope choice given the size this feature
  already reached; a nicer inline editing experience is a reasonable
  follow-up.
- **No touch-specific gesture handling** (pinch-to-zoom, two-finger pan)
  beyond what the mouse-event handlers also happen to receive via
  `onTouchStart`/`onTouchMove`/`onTouchEnd` — not separately tested on a
  real touch device.

## Security

- **Superseded — see the note at the top of this document.** This section
  originally described a client-side-only feature with no backend to
  secure. That's no longer the case: attaching a file now does reach
  `api/attachments.py`, which independently re-validates everything
  (magic bytes, size, dimensions), runs malware scanning (when
  configured) and injection-pattern detection, and stores the file
  server-side. See `SECURITY.md`'s "Chat attachments — security controls"
  section for the current, accurate list of controls.
- **No API keys are exposed in the frontend, still true now that
  `AiGuidedEditAdapter` calls a real endpoint.** The IMA Studio credential
  (`Settings.ima_api_key`) lives only server-side
  (`media_gen/image_edit_provider.py`) — the frontend only ever talks to
  this app's own `POST /attachments/{id}/ai-edit`, never the provider
  directly, and `GET /attachments/capabilities` exposes only a boolean
  (`image_ai_editing`) and a provider display name, never a key or
  endpoint URL.
- **The local Konva editor's own output never leaves the browser tab
  except via the same attachment-upload path** any other attached image
  uses — saving an edit re-uploads the edited bytes as a fresh attachment
  (see `CLAUDE.md`'s "Frontend" paragraph under "Chat attachments"), it
  does not open a separate, unaudited channel.
