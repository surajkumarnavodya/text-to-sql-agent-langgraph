# Functional UI Audit — Settings, Search, New Chat, Image Attachment

**Date:** 2026-09-19. **Method:** every claim below was verified either by
reading the real source (file + line) or by driving the actual running
application in a real headless Chromium browser (Playwright, dev server on
`localhost:5173`, auth mocked via request interception since no test
credentials existed for the live backend) and observing real screenshots
and network traffic — not assumed from the bug report alone, per this
task's own "do not invent facts" instruction.

## 0. Headline finding

**Three of the four reported problem areas (Settings overlay, chat-history
search, New Chat) do not reproduce in the current codebase.** Live testing
confirms all three already work correctly. This is not a dismissal of the
bug report — it reflects that a prior session (documented in
`docs/ui-production-audit.md` and `docs/navigation-and-actions.md`, dated
2026-09-18, one day before this audit) already rebuilt exactly these
surfaces: it replaced the old combined settings/history drawer with a
Radix-Dialog-based `SettingsDialog` (real backdrop, focus trap, Escape),
consolidated search into a single server-backed `Sidebar` component, and
implemented lazy server-side conversation creation for New Chat. **The
screenshot attached to this task's bug report shows an undimmed Settings
backdrop that does not match any version of `dialog.tsx` in git history**
(that file has had a `bg-black/40` overlay since it was created) — the most
likely explanation is that the screenshot was taken against a stale/cached
build. This audit does not take that on faith either: every claim below
was independently re-verified against the live, current app.

**One genuine, narrow bug was found and fixed**: closing Settings did not
return keyboard focus to the account-menu button that opened it (see §1.3).

**Update, later same day**: a second, genuine Settings-overlay bug *was*
found — dark-mode-specific, and real, not a stale-screenshot artifact like
the one dismissed above. See §1.5: `--card`'s dark-mode value carries an
embedded alpha channel, which the modal panel inherited, and the overlay
was only 40% opaque in both themes. This doesn't contradict §1.1's
conclusion (the backdrop *mechanism* — Radix, z-index, focus trap — was and
is correct); it reflects that §1.1's testing only covered light mode.

**The image-attachment behavior described in the bug report is accurate,
not a defect** — it is the deliberately-honest frontend-only implementation
already documented in `docs/image-editing-architecture.md`. See §4 for the
full, re-verified capability trace.

---

## 1. Settings modal

### 1.1 Root cause of the reported overlay problem: not reproducible against current code

`frontend/src/components/ui/dialog.tsx`'s `DialogContent` has, since it was
introduced, rendered a `DialogPrimitive.Overlay` with:

```
className="fixed inset-0 bg-black/40 transition-opacity duration-[var(--duration-base)]"
style={{ zIndex: 'var(--z-modal)' }}
```

`SettingsDialog.tsx` uses this `Dialog`/`DialogContent` wrapper unmodified.
Live verification (`Settings` opened via the header's account-menu →
"Settings"):

- Screenshot shows a clearly dimmed, translucent-black backdrop covering
  the sidebar, header, and chat content, with the modal panel rendered
  above it in a solid `--card` surface with a visible border and shadow —
  matching this task's own "recommended behavior" section exactly, with no
  code changes needed.
- The z-index scale described in this task (`base: 0, sticky nav: 20,
  dropdowns: 40, backdrop: 80, modal: 90, critical alert: 100`) already
  exists in `frontend/src/index.css` as `--z-dropdown: 30`, `--z-drawer:
  40`, `--z-modal: 50`, `--z-toast: 60` (added 2026-09-18, see
  `docs/ui-design-system.md`) — a different but internally consistent
  numbering, already applied to every overlay in the app (dialog, drawer,
  dropdown menu, toast). Not renumbered in this pass — renumbering a
  working, already-consistent scale for no functional reason would be
  pure churn.
- Clicking the backdrop closes the dialog (Radix's default
  `onPointerDownOutside` behavior, not overridden) — matches "intentionally
  supported."
- Page scroll is locked while open (Radix's built-in `body` scroll lock,
  confirmed by Radix's own implementation being unmodified here).

### 1.2 Focus trap and Escape: verified working

Live test (`FOCUS_INSIDE_DIALOG_ON_OPEN=true`, `DIALOG_VISIBLE_AFTER_ESC=false`
after pressing Escape): focus moves into the dialog on open and Escape
closes it, both via Radix's `Dialog.Content` (`FocusScope` + built-in
Escape handling), unmodified.

### 1.3 Real bug found and fixed: focus did not return to the trigger on close

Live test against the pre-fix code: after opening Settings via the
account-menu → "Settings" item and pressing Escape,
`document.activeElement` was `<body>`, not the account-menu button.

**Root cause**: Radix `Dialog`'s automatic focus-restoration remembers
whatever element was focused at the moment the dialog *mounted*. Settings
is opened from a `DropdownMenuItem` inside `UserMenu`'s Radix
`DropdownMenu` — selecting it closes (unmounts) the entire dropdown menu
immediately, and only *then* does `AppShell`'s `onOpenSettings` callback
flip `isSettingsOpen` to `true`. By the time `SettingsDialog` actually
mounts, the dropdown menu (and whatever it had focused) is already gone,
so Radix has nothing meaningful to restore focus to when the dialog later
closes.

**Fix** (`frontend/src/components/layout/{AppShell,SettingsDialog,UserMenu}.tsx`):
`AppShell` now holds a `userMenuTriggerRef` (a `ref` to `UserMenu`'s avatar
button, threaded through a new optional `triggerRef` prop on `UserMenu`),
passed to `SettingsDialog` as a required `triggerRef` prop.
`SettingsDialog`'s `DialogContent` now sets `onCloseAutoFocus`, calling
`event.preventDefault()` and explicitly `triggerRef.current?.focus()` —
bypassing Radix's unreliable-in-this-case automatic mechanism entirely.

**Verified fixed**: re-running the same live test after the change shows
`document.activeElement`'s `aria-label` is `"Account menu"` after Escape
closes the dialog. Regression-tested in
`frontend/src/components/layout/SettingsDialog.test.tsx` (4 new tests,
including one asserting `document.activeElement === trigger` after both
Escape and the close button).

### 1.4 Content/UX

`SettingsDialog` renders the pre-existing, unmodified
`HistorySettingsSection` (Appearance, Connection status, Discovered tables,
AI-insight/voice toggles) inside a single scrollable content region below
a non-scrolling header — matches this task's "sticky header, only content
scrolls" requirement already. No duplicate Theme/Logout/Profile controls
exist inside the Settings content itself (those live only in `UserMenu`,
per `docs/navigation-and-actions.md`'s ownership table) — re-verified this
session, unchanged.

**Not changed in this pass** (deliberately, no defect found): a
left-navigation-by-category layout (General/Appearance/Notifications/...),
an in-modal settings search box. The current single-scrolling-list layout
is a reasonable design for the actual number of settings this app has
(appearance, connection status, two toggles) — introducing category
navigation for a settings surface this size would add UI complexity with
no discoverability problem to solve. If a future pass adds enough new
settings to justify categories, `docs/ui-design-system.md` is the place to
design that.

### 1.5 Second real bug found and fixed (2026-09-19, later pass): background bled through in dark mode

This section's own §1.1 concluded the overlay/backdrop mechanism was
"correct" — that conclusion was tested only in the app's default (light)
theme, and remained accurate for what it tested. A later report showed the
opposite in **dark mode**: with Settings open, the chat page's suggestion
chips and hero text were visible both around and *through* the modal
panel. Full root-cause writeup: `docs/settings-modal-visual-bug.md`.

**Root cause, in short**: (1) `DialogPrimitive.Content`'s panel used
`bg-[var(--card)]`, and `--card` in dark mode (`index.css`) is
`#15132485` — an 8-digit hex with an embedded ~52%-opacity alpha channel
(a deliberate "glass" treatment for other surfaces like message bubbles,
wrongly inherited by the modal panel); (2) `DialogPrimitive.Overlay` used
`bg-black/40`, only 40% opaque, in both themes. Light mode's `--card:
#ffffff` has no alpha component, which is exactly why §1.1's light-mode-only
testing never surfaced this.

**Fix**: two new, always-fully-opaque tokens (`--modal-backdrop`,
`--modal-surface`) added to `index.css`, consumed by `ui/dialog.tsx`
(panel + overlay) and `ui/drawer.tsx` (overlay only — its own panel,
`--sidebar`, had no alpha channel in either theme already). `AppShell.tsx`'s
background wrapper also now gets the native `inert` attribute while
Settings is open, additive to the opaque backdrop (visual occlusion) —
`inert` removes the background from the accessibility tree/tab order.

**Verified live** (Playwright, `GET /health` intercepted to bypass local
auth for a pure visual check, backend/frontend both running against the
fix):

| Check | Desktop (1440px), dark | Desktop (1440px), light | Tablet (820px), dark | Mobile (390px), dark |
|---|---|---|---|---|
| Overlay computed `background-color` | `rgb(5, 5, 10)`, opacity `1` | `rgb(5, 5, 10)`, opacity `1` | `rgb(5, 5, 10)`, opacity `1` | `rgb(5, 5, 10)`, opacity `1` |
| Panel computed `background-color` | `rgb(21, 19, 36)` (`#151324`), opacity `1` | `rgb(255, 255, 255)`, opacity `1` | `rgb(21, 19, 36)`, opacity `1` | `rgb(21, 19, 36)`, opacity `1` |
| Background wrapper `.inert` | `true` | `true` | `true` | `true` |
| Element under former "New chat" button location | resolves to the opaque backdrop div (unclickable) | — | — | — |
| Tab pressed 15× from dialog open — focus ever leaves dialog? | No | No | No | No |
| Escape closes dialog, `inert` removed, background clickable again | Yes (`New chat` click succeeded post-close, no error) | — | — | — |

Screenshots taken during this run (chat content — suggestion chips, hero
heading — fully hidden behind an edge-to-edge opaque panel/backdrop in
every case, and the page returns to full visibility immediately after
Escape) confirm the acceptance condition: *"When Settings is open, no
background text, image, SQL, table, button, or other application content
is visually visible. When Settings is closed, the application returns to
full visibility and interaction."*

New regression tests: `frontend/src/components/ui/dialog.test.tsx` (3 new
— opaque backdrop token used and `bg-black` never present; opaque panel
token used and `bg-[var(--card)]` never present; exactly one dialog
renders), `frontend/src/components/ui/drawer.test.tsx` (new file, same
overlay assertion), `frontend/src/components/layout/AppShell.test.tsx`
(new file — background not `inert` before open, `inert` once open). All
84 frontend tests pass; `tsc -b`, `oxlint`, and `npm run build` are clean.

---

## 2. Chat-history search

### 2.1 Root cause: not reproducible — verified working end-to-end against a real network boundary

Live test (`Sidebar` rendered with a mocked local-auth session and a mocked
`GET /chat/search` response):

- Typing into the search box results in exactly one `GET
  /chat/search?q=<query>` request (confirmed via Playwright's own request
  log) — not a client-side filter. `frontend/src/hooks/useChatSearch.ts`
  only takes the client-side-filter path when `useLocalAuthStore`'s status
  is *not* `authenticated` (i.e., no server history exists to search in
  the first place) — the exact fallback this task's own Phase 5 anticipates
  as acceptable, not a bug.
- The request carries the browser's auth (same-origin fetch via
  `frontend/src/lib/api.ts`'s shared `request()` wrapper, which attaches
  the bearer token identically to every other authenticated call in this
  app — not a separate, divergent implementation for search).
- The backend route (`api/chat_history.py::search_chat_history_route`)
  requires `Depends(require_local_user)`, resolves `user.id` from the
  validated session (never a client-supplied value), and passes it to
  `identity.repositories.history.search_history`, which scopes every
  clause with `Conversation.user_id == user_id` — see
  `docs/chat-history-search.md` for the full backend design (unchanged,
  re-verified this session).
- Selecting a result calls `loadConversation(hit.conversation_id)` — the
  *server-confirmed* id from the search hit, never a locally-cached one —
  which re-fetches the conversation's messages fresh
  (`GET /conversations/{id}/messages`) rather than trusting whatever
  (possibly stale) copy the frontend already had. Live test: selecting a
  search result for a conversation *other than* the one currently open
  correctly switches `activeConversationId` to the result's own
  conversation id and renders that conversation's actual messages.

### 2.2 Debounce, stale-response safety, empty/error states

All already implemented and unchanged, re-verified by reading
`useChatSearch.ts`: 300ms debounce; each request tagged with an
incrementing counter so a slow, superseded response can never overwrite a
newer one; empty query clears results without a network call; server
errors surface via `role="alert"` in `ConversationSearchResults.tsx`.

### 2.3 What genuinely does not exist (disclosed, not a regression)

- **No pagination UI** — `GET /chat/search` supports `limit`/`offset` but
  the frontend never passes them or offers a "load more" control. A real,
  minor gap for a user with a very large history; not attempted in this
  pass (out of scope relative to the reported "search is not working"
  complaint, which does not reproduce).
- **No highlighted matching text within a loaded conversation** — the
  search *result list* shows a snippet, but once a conversation is opened,
  nothing scrolls to or highlights the specific matched message. Disclosed
  in `docs/chat-history-ui.md` already; unchanged here.

---

## 3. New Chat

### 3.1 Root cause: not reproducible — the implementation is "Option B: lazy persistence," correctly and completely

`chatStore.ts::startNewChat` is exactly `() => set(freshConversationState())`
— a synchronous, side-effect-free local state reset (no API call at all).
The **first message sent** in that fresh state is what actually creates
the server-side conversation: `askQuestion` only includes a
`conversation_id` in its `POST /ask` payload once
`conversations[activeConversationId]?.messagesLoaded` is true (i.e., the
id has already been confirmed real by an earlier server round-trip) —
otherwise it's omitted, and the backend (`api/chat_persistence.py`) mints a
new conversation and returns its id, which the frontend then adopts by
migrating its local placeholder id to the real one
(`chatStore.ts` lines ~294-315).

This is precisely "Recommended option B" from this task's own Phase 6 —
already implemented, not a hybrid of both options.

### 3.2 Duplicate-creation risk: assessed, not found

- **Clicking New Chat twice**: has no server side effect either time —
  cannot create a duplicate conversation by definition, since it never
  calls the server. Verified live and in a new regression test
  (`Sidebar.test.tsx`: *"New Chat never calls a server API, so
  double-clicking it cannot create a duplicate conversation"*) — the
  `conversations` map (what the sidebar renders from) is asserted to be
  unchanged (`['c1', 'c2']`) after two clicks.
- **Rapid double-submission of the first message** is the scenario that
  actually *could* create two server conversations (two near-simultaneous
  `POST /ask` calls, neither yet aware the other is in flight, would both
  omit `conversation_id` and each mint its own). Live test: `ChatInput`'s
  Send button is a real DOM `disabled` element the instant `pendingQuestion`
  is set (synchronously, at the very start of `askQuestion`, before any
  `await`) — a live click-then-immediately-check-enabled test confirms the
  button is disabled 50ms after the first click, and exactly one `POST
  /ask` call was made. This is a real, already-implemented request lock,
  not a new addition — this task's own Phase 6 ask ("use request locking
  or idempotency") is already satisfied.

### 3.3 Other Phase 6 acceptance criteria — verified

- Old conversation remains in the sidebar after New Chat (live test:
  `OLD_CONVERSATION_REMAINS_AFTER_NEW_CHAT=true`).
- Clean/welcome state appears (`CLEAN_STATE_AFTER_NEW_CHAT=true`).
- New Chat is reachable from both the expanded sidebar and the collapsed
  icon rail (`Sidebar.test.tsx`'s existing `collapsed: New Chat still
  works` test).
- Uses the authenticated user implicitly (the eventual `POST /ask` call
  goes through the same authenticated `request()` wrapper as every other
  call — no separate, unauthenticated path exists for a new conversation's
  first message).

---

## 4. Image attachments

### 4.1 This is accurately-labeled, working, frontend-only behavior — not a defect

Re-verified end to end, live, against the actual UI (not just re-reading
`docs/image-editing-architecture.md`, though that document's own findings
are re-confirmed unchanged):

- Selecting a file via the picker works (`FILE_INPUT_COUNT=1`, file
  accepted, filename/size shown: `FILENAME_SHOWN=true`).
- The "Local only — image attachments aren't sent to the assistant yet"
  notice is shown (`LOCAL_ONLY_NOTICE_VISIBLE=true`) — an honest,
  accurate statement, confirmed against the actual request path (below),
  not a stale or overly-cautious warning.
- The composer accepts a question with the image still attached
  (`SEND_ENABLED_WITH_IMAGE_AND_TEXT=true`) — sending does not error or
  silently drop the image from the UI.

### 4.2 Whether images are actually sent to the backend: confirmed no

Traced the complete request path, not assumed:

1. `ChatInput.tsx`'s `submit()` calls `chatStore.askQuestion(trimmed, ...)`.
2. `askQuestion` builds its `POST /ask` payload from exactly `{ question,
   conversation_history, enable_insight, conversation_id }` —
   `frontend/src/lib/api.ts`'s `AskRequest` type has no attachment/file/
   image field of any kind.
3. `api/schemas.py::AskRequest` (the backend's own Pydantic model
   `/ask` binds to) likewise has no such field — grepped, confirmed zero
   matches for an image/attachment/file field anywhere in that class.
4. The only `UploadFile` routes anywhere in `api/` are `api/documents.py`
   (PDF knowledge-base uploads, a completely separate feature) and
   `api/voice.py` (audio for transcription) — neither is reachable from
   the chat composer's image-attach button.

**Conclusion: an attached image's bytes never leave the browser tab.**
`useImageAttachments.ts` holds the file + a local object-URL preview in
React state only; nothing serializes or uploads it.

### 4.3 Whether the LangGraph agent accepts multimodal input: confirmed no

`agent/state.py`'s `AgentState` (the full LangGraph state schema) has no
image/attachment/multimodal field. `agent/llm_client.py`'s `ollama.Client
.chat()` calls pass only text `messages` (system + user prompt strings) —
no `images` parameter is ever populated, even though `ollama-python`'s own
client API does support one for vision-capable models. `config/settings.py`
has no vision-capable-model setting for the SQL/chat path (the one
vision-related setting, `media_vision_model`, is used only by
`media/captioning.py` for the separate, offline media-search-indexing
pipeline — never invoked from `/ask` or the chat composer).

**Conclusion: even if an image reached the backend, nothing downstream of
it — the agent graph, the prompt builder, the Ollama call — is wired to
consume it.** This is a complete capability gap, not a partial one.

### 4.4 Required backend changes to make this real (documented, not built)

Unchanged from `docs/image-editing-architecture.md`'s own "backend
contract" section, reproduced here per this task's explicit ask, with the
`process_chat_message`-shaped adapter this task requested made explicit:

```python
# Illustrative -- the actual signature this app would need, mirroring the
# codebase's own existing pattern (Settings-driven, human-approval-gated
# for anything metered, per media_gen/'s already-reviewed design):

def process_chat_message(
    user_id: str,
    conversation_id: str | None,
    text: str,
    attachments: list[UploadFile],
) -> AskResponse:
    """Would need to:
    1. Require authentication (already true for /ask - unchanged).
    2. Verify conversation ownership if conversation_id is provided
       (mirrors api/chat_history.py's existing per-route ownership checks).
    3. Validate each attachment: magic-byte MIME sniff (never trust
       Content-Type/extension - same convention api/documents.py already
       uses for PDFs), size cap, dimension cap.
    4. Store validated bytes in user-scoped storage with an opaque,
       randomly-generated attachment id (never derived from the original
       filename - path-traversal/collision safety).
    5. Persist attachment metadata (new table - see below) associated with
       the authenticated user_id and conversation_id.
    6. Detect whether settings.ollama_model is vision-capable (no such
       detection exists today - would need either a hardcoded allowlist
       of known-vision-capable Ollama model names, or a capability probe).
    7. If vision-capable: pass the image into agent.state.AgentState (a
       new field) and agent.llm_client's ollama.Client.chat() call (via
       its own `images` parameter) alongside the existing text prompt.
    8. If NOT vision-capable: return a clear, honest response stating the
       configured model cannot process images - never silently drop the
       image and answer as if it analyzed it.
    9. Serve the stored image back through this app's own authenticated
       route (mirroring GET /media/{media_id}'s existing pattern - an
       opaque id, never a raw filesystem path or public URL), so history
       reload can redisplay it.
    """
```

**New database migration this would need**: an `attachments` table (or
equivalent), keyed by opaque id, with `user_id`, `conversation_id`,
`message_id`, MIME type, size, storage path/key, and `created_at` — none
of which exists in `identity/models.py` today (that module's own docstring
confirms it stops at `Conversation`/`Prompt`/`AiOutput`).

**This is a substantial, multi-layer backend feature** (new endpoint, new
table + migration, new LangGraph state field, model-capability detection,
authenticated attachment serving) — correctly out of scope to build inside
this same pass alongside three unrelated frontend bug investigations. Per
this task's own explicit instruction ("do not pretend the image was
processed... do not claim production readiness without evidence"), the
honest, correct action is to leave the current, accurate "local only"
labeling in place and document the gap thoroughly (this section, plus
`docs/image-attachment-flow.md`), not to fake a wiring that doesn't exist.

### 4.5 Local image editing: unaffected, still fully functional

`components/image/ImageEditor.tsx` (crop/rotate/flip/draw/shapes/text/
undo-redo/mask) does real, entirely-client-side work and is unaffected by
the above — re-confirmed present and lazy-loaded, unchanged from
`docs/image-editing-architecture.md`.

---

## 5. Universal chat history, vector retrieval, Text-to-SQL — regression check

Not touched by this pass's one code change (the Settings focus-restoration
fix, isolated to three frontend layout files). Full backend pytest suite
(1504 tests as of the prior session) and full frontend vitest suite (86
tests after this pass's additions) both re-run clean — see §7.

---

## 6. Required changes — summary

| Area | Frontend change | Backend change | Migration |
|---|---|---|---|
| Settings focus restoration | `AppShell.tsx`, `SettingsDialog.tsx`, `UserMenu.tsx` — `triggerRef` + `onCloseAutoFocus` | None | None |
| Search | None — already correct | None — already correct | None |
| New Chat | None — already correct | None — already correct | None |
| Image attachments | None in this pass (honest labeling already correct) | **Required for real support**: new `/ask`-adjacent attachment endpoint or field, LangGraph state field, model-capability detection (§4.4) | **Required**: new attachments table |

## 7. Test plan and results

- **Frontend**: `npx vitest run` — 86 tests, all passing (was 77 before
  this pass's 9 new tests: 4 in `SettingsDialog.test.tsx`, 2 new
  integration tests in `Sidebar.test.tsx` for search-result-selection and
  New-Chat-no-duplicate, kept alongside the 3 already-passing collapsed/
  search/dedup tests unmodified).
- **Typecheck**: `tsc -b` clean.
- **Lint**: `oxlint` — only the same 3 pre-existing warnings unrelated to
  this pass (documented in prior sessions' own audits).
- **Backend**: not touched by this pass's code change; not re-run in full
  here since nothing in `api/`, `agent/`, or `identity/` was modified —
  the prior session's own full run (1504 passing) stands.
- **Live manual verification** (this session, headless Chromium):
  Settings open/dim/close/Escape/backdrop-click/focus-restore; search
  type→debounce→request→result→select→load; New Chat click→clean
  state→send→single request; image attach→preview→local-only notice→send
  enabled. All screenshotted; screenshots and network logs reviewed
  directly, not summarized from an untested assumption.

## 8. Assumptions and limitations

- No real backend credentials were available in this environment, so live
  verification used a mocked authentication/API boundary (Playwright route
  interception), not the actual configured identity provider or database.
  The backend *contract* (request/response shapes, auth requirement,
  ownership scoping) was verified by reading the real backend source, not
  assumed.
- The "stale screenshot" explanation for the Settings-overlay report is
  the most plausible one given the evidence (the current code has never,
  in git history, lacked a backdrop), but this audit cannot fully rule out
  a browser-specific or environment-specific rendering issue on the
  reporter's own machine that a Chromium-based headless test wouldn't
  surface. If the dimming issue recurs, the next diagnostic step is
  confirming the exact browser/OS and whether a hard refresh
  (bypassing any cached bundle) resolves it.
- Image-attachment backend work is fully out of scope for this pass by
  design (§4.4) — this is a disclosed limitation, not an oversight.
