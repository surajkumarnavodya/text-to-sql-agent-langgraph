# Image Editing Architecture

**Status: implemented and tested (local editing) / not connected to a
backend (AI-guided editing).** Read this before assuming "AI-guided
editing" in the composer actually calls a model — it does not, by design,
and says so in the UI.

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

## What does NOT exist: AI-guided editing

**No backend endpoint accepts an image for AI-guided editing.** Confirmed
by inspection: `api/schemas.py`'s `AskRequest` has no file/image field of
any kind, and the only `UploadFile` routes anywhere in `api/` are PDF
documents (`api/documents.py`) and voice audio (`api/voice.py`).
`media_gen`'s existing image *generation* takes a text prompt and produces
a brand-new image — it does not accept an uploaded image plus an edit
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

### The backend contract a real implementation would need

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

- **Attachments are never actually sent anywhere.** `AskRequest` has no
  image field, so an attached/edited image is visual-only, local to the
  browser tab — the composer shows an explicit
  "Local only — image attachments aren't sent to the assistant yet"
  notice whenever one is attached. This is the single most important
  thing to understand about this feature's current state.
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
- **The mask sent to `AiGuidedEditAdapter` is currently the whole
  flattened canvas**, not a true isolated render of just the mask layer
  — a real second offscreen render would be needed for a production
  implementation; noted directly in the code
  (`ImageEditor.tsx::flattenMaskOnly`).
- **Text annotation uses `window.prompt()`**, not an inline canvas text
  editor — a deliberate, simpler scope choice given the size this feature
  already reached; a nicer inline editing experience is a reasonable
  follow-up.
- **No touch-specific gesture handling** (pinch-to-zoom, two-finger pan)
  beyond what the mouse-event handlers also happen to receive via
  `onTouchStart`/`onTouchMove`/`onTouchEnd` — not separately tested on a
  real touch device.

## Security

- **Client-side validation is a UX convenience, not a security
  boundary.** There is no backend endpoint to receive an uploaded image
  yet, so there is nothing to layer server-side re-validation onto today.
  When a real upload endpoint is built, it **must** independently
  re-validate everything checked client-side (magic bytes, size,
  dimensions) — exactly the pattern `api/documents.py` already
  establishes for PDF uploads — never trust the client-side check alone.
- **No API keys are exposed in the frontend.** `AiGuidedEditAdapter`
  contains no credentials — it cannot, since it never reaches a real
  provider.
- **Images never leave the browser tab.** No image data is uploaded,
  stored, or transmitted anywhere by this feature as currently built.
