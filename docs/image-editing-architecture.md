# Image Editing Architecture

> **⚠️ Partially superseded — read the note below before relying on this
> document.** This document's core claim when written was "attachments are
> never actually sent anywhere" (see "Known limitations" below). That is no
> longer true: a subsequent feature pass built a real backend attachment
> pipeline (`attachments/`) — an attached image now genuinely reaches a
> configured vision model or OCR, and explicit `POST
> /attachments/{id}/extract-text|resize|remove-text` actions exist for
> real, deterministic image operations. See `CLAUDE.md`'s "Chat
> attachments"/"Explicit image actions" sections and `docs/API.md`'s
> "Attachments" section for the current, accurate state. **What remains
> genuinely accurate below**: the local Konva-based editor's own feature
> set (crop/rotate/draw/annotate), its library-choice rationale, and —
> specifically — the **"AI-guided editing" panel** (a natural-language-
> prompted *generative* edit, distinct from the real "Resize"/"Remove
> text" actions above): it is still a deliberate, permanent stub exactly
> as described here, unaffected by the later attachment-pipeline work.

**Status: implemented and tested (local editing, plus a real backend for
OCR/vision/resize/text-removal — see the note above) / still not connected
to a backend (AI-guided editing specifically).** Read this before assuming
"AI-guided editing" in the composer actually calls a model — it does not,
by design, and says so in the UI.

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

## What does NOT exist: AI-guided (generative, prompt-driven) editing

**Update, later pass:** the claim immediately below ("no backend endpoint
accepts an image") is no longer accurate in general — `api/attachments.py`
now has real `UploadFile` routes (`POST /attachments/upload`, plus
`POST /attachments/{id}/resize|remove-text`), and `AskRequest` does now
carry `attachment_ids`. What remains true, specifically, is narrower: no
endpoint accepts an image **plus a free-text natural-language edit
instruction** and returns a generatively-edited result — `POST
/attachments/{id}/remove-text` is real pixel editing, but it takes explicit
region coordinates, not a prompt, and uses classical (non-generative)
inpainting. See `CLAUDE.md`'s "Chat attachments"/"Explicit image actions"
sections for what's real today; the rest of this section (the
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

- **Superseded — see the note at the top of this document.** This section
  originally described a client-side-only feature with no backend to
  secure. That's no longer the case: attaching a file now does reach
  `api/attachments.py`, which independently re-validates everything
  (magic bytes, size, dimensions), runs malware scanning (when
  configured) and injection-pattern detection, and stores the file
  server-side. See `SECURITY.md`'s "Chat attachments — security controls"
  section for the current, accurate list of controls.
- **No API keys are exposed in the frontend.** `AiGuidedEditAdapter`
  (the *generative* AI-guided editing stub, not the real attachment
  upload path above) contains no credentials — it cannot, since it never
  reaches a real provider.
- **The local Konva editor's own output never leaves the browser tab
  except via the same attachment-upload path** any other attached image
  uses — saving an edit re-uploads the edited bytes as a fresh attachment
  (see `CLAUDE.md`'s "Frontend" paragraph under "Chat attachments"), it
  does not open a separate, unaudited channel.
