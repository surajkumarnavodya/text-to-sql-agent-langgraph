# Image Attachment Flow — Current State vs. Required for Real Support

> **⚠️ Superseded — kept for historical record only.** Every "❌ Does not
> happen" / "N/A" row in the table below described a real gap at the time
> this document was written, but a subsequent feature pass (`attachments/`,
> `POST /attachments/upload`, `POST /ask`'s `attachment_ids`) built the
> entire missing pipeline this document says doesn't exist: attachments
> now do reach the backend as real bytes, are validated/malware-scanned/
> stored/processed, and reach a configured vision model or OCR via a
> dedicated LangGraph subgraph (`attachments/graph.py`). A later pass also
> added explicit `POST /attachments/{id}/extract-text|resize|remove-text`
> actions. **Do not use this document as the current state of the image
> attachment pipeline** — see `CLAUDE.md`'s "Chat attachments, image
> actions, and their security hardening" section, `docs/API.md`'s
> "Attachments" section, and `SECURITY.md`'s "Chat attachments — security
> controls" section
> instead. **Update, 2026-09-27**: the image **editor**'s own "AI-guided
> editing" panel (a natural-language-prompted generative edit, distinct
> from the real "Resize"/"Remove text" actions above) is also no longer a
> stub — it now calls a real provider (IMA Studio) via `POST
> /attachments/{id}/ai-edit` — see `docs/image-editing-architecture.md`
> for the current, accurate design.

Companion to `docs/functional-ui-audit.md` §4 (the full investigation) and
`docs/image-editing-architecture.md` (the editor itself). This document is
the step-by-step flow this task's Phase 7 asked for, with each step marked
against what was actually verified in the current codebase.

## The flow, expected vs. actual

| # | Expected step | Actual status |
|---|---|---|
| 1 | User selects or drops an image | ✅ Works — file picker and the underlying `useImageAttachments` hook both function |
| 2 | Frontend validates the file | ✅ Works — `lib/imageValidation.ts`: magic-byte MIME sniff (not extension/`Content-Type`), size cap, dimension cap |
| 3 | Frontend displays a local preview | ✅ Works — thumbnail, filename, size shown in `AttachmentChip` |
| 4 | Frontend sends the file or attachment reference to backend | ❌ **Does not happen** — no network call is ever made for the image; verified by reading `askQuestion`'s actual `POST /ask` payload construction (§4.2 of the audit) |
| 5 | Backend validates the file | N/A — nothing reaches the backend |
| 6 | Backend stores or processes it securely | N/A |
| 7 | Backend associates it with the authenticated user and conversation | N/A |
| 8 | Backend passes it to LangGraph | N/A |
| 9 | Agent processes image and prompt | N/A |
| 10 | User and assistant messages are persisted | Partial — the **text** question and answer persist normally (existing chat-history behavior, unaffected); no attachment record exists to persist since none is created |
| 11 | Frontend receives response | ✅ Works — the text-only response returns and renders normally |
| 12 | Frontend displays attachment and response | ✅ Works for the *local* attachment (still visible in the composer/turn) and the response; the attachment was never "sent" so there's nothing server-side to redisplay after a reload |
| 13 | History reloads attachment correctly | ❌ Not possible — nothing was persisted server-side, so a reloaded conversation never shows the image (only the text messages) |

## Root cause of steps 4–9, 13 not happening

Not a bug — a real, confirmed capability gap, traced completely (see the
audit's §4.2/§4.3 for the exact grep/read evidence):

- `AskRequest` (frontend `lib/api.ts` and backend `api/schemas.py`) has no
  attachment/file/image field.
- No `UploadFile` route in `api/` is reachable from the chat composer (the
  only two that exist — `api/documents.py`, `api/voice.py` — serve
  unrelated features).
- `AgentState` (`agent/state.py`) has no image field, and
  `agent/llm_client.py`'s Ollama calls never populate `images` (the
  parameter `ollama-python`'s own client supports for vision models).

**The configured model's own vision capability was never the blocker** —
the request never gets far enough to reach a model-capability check at
all, since the plumbing to carry an image from browser to Ollama call
doesn't exist yet at any layer.

## What would need to be built (not built in this pass)

See `docs/functional-ui-audit.md` §4.4 for the full `process_chat_message`-
shaped adapter contract, the new attachments table, and why this is
correctly out of scope for a single pass alongside unrelated bug
investigations. Summary of the layers needed, in dependency order:

1. **Attachment storage + metadata table** (new migration under
   `identity/migrations/` or a sibling module, following `rag/store.py`'s
   or `media/store.py`'s existing "dedicated table, opaque id, user-scoped"
   pattern rather than inventing a new convention).
2. **An authenticated upload/attach endpoint** (or an extended `/ask`
   accepting `multipart/form-data`), with magic-byte validation, size/
   dimension limits, and ownership checks — mirroring `api/documents.py`'s
   already-reviewed PDF-upload security posture.
3. **A vision-capability signal** for the configured `OLLAMA_MODEL` (no
   such detection exists today — would need either a maintained allowlist
   of known vision-capable model name patterns, e.g. `llava`, `bakllava`,
   or a documented operator-set config flag; auto-probing isn't reliable
   across arbitrary Ollama models).
4. **`AgentState` + `agent/llm_client.py` changes** to carry image bytes
   (or a reference to them) into the `ollama.Client.chat()` call's own
   `images` parameter, gated on step 3's capability signal — if the
   configured model isn't vision-capable, the response must say so
   explicitly rather than silently answering text-only while implying the
   image was considered.
5. **Authenticated attachment serving** for history reload, mirroring
   `GET /media/{media_id}`'s existing opaque-id pattern (never a raw
   filesystem path or public URL).

## What this means for the UI today (unchanged, confirmed correct)

The composer's "Local only — image attachments aren't sent to the
assistant yet" notice is accurate and should **not** be removed or
softened until step 2 above genuinely exists — removing it while attaching
an image still does nothing server-side would make the UI actively
misleading, which this task's own instructions explicitly forbid ("do not
claim that image attachments are sent to the agent unless you verified the
complete request path").
