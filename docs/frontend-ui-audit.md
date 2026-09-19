# Frontend UI Audit

**Date:** 2026-09-18. Every claim below was verified by reading real source in this
session (two research passes plus direct file/dependency inspection) — not
inferred from `CLAUDE.md` prose alone. This was Phase 1 of the UI-transformation
request, written before any implementation — kept as-is below as a point-in-time
"before" snapshot, not updated to match what shipped. For the "after" —
what was actually built in response to the gaps this audit found — see
[`docs/ui-design-system.md`](ui-design-system.md),
[`docs/chat-history-ui.md`](chat-history-ui.md), and
[`docs/image-editing-architecture.md`](image-editing-architecture.md).

---

## 1. Current Component Hierarchy

```
App.tsx (react-router-dom)
├── /auth/callback → AuthCallback (outside AuthGate — OIDC redirect target)
└── AuthGate → AppShell
    ├── /                → Chat.tsx
    │     ├── SuggestedPrompts.tsx        (empty-state prompts)
    │     ├── TurnCard.tsx (× N)           one Q+A turn
    │     │     ├── ChatMessage.tsx
    │     │     ├── SchemaContextPanel.tsx, QueryPlanPanel.tsx (collapsible)
    │     │     ├── SqlEditor.tsx          (CodeMirror, read-only SQL box)
    │     │     ├── ResultsTable.tsx (@tanstack/react-table), ResultChart.tsx (chart.js)
    │     │     ├── SourcesUsedPanel.tsx → SourceAnswerCard.tsx / MediaResultCard.tsx / MediaSearchResultCard.tsx
    │     │     ├── RetryTimeline.tsx, TimingBadge.tsx, GoldenFeedbackWidget.tsx
    │     │     └── CopyAnswerButton.tsx, DownloadAnswerButton.tsx
    │     ├── PendingTurn.tsx              (in-flight state, no modal/overlay)
    │     └── ChatInput.tsx                (composer: textarea + mic + send only)
    ├── /knowledge-sources → KnowledgeSources.tsx (PDF upload/list, tabs: documents/policies)
    └── /media-search       → MediaSearch.tsx (standalone content search page)

AppShell.tsx (shell for all 3 interior routes)
├── header (product mark, nav, ThemeToggle, gear icon)
├── main (routed page content)
└── HistoryDrawer.tsx (gear-toggled, closed by default)
      ├── conversation list (search box, New Chat, per-item rename/delete)
      └── HistorySettingsSection.tsx
            ├── ThemeToggle, AccentColorPicker, FontPicker, LanguageSelector
            ├── per-database connection status + "Test Connection" + "Refresh Schema"
            ├── schema table browser
            └── AI-insight toggle, voice-mode toggle, "Install app" button
```

Reusable primitives (`src/components/ui/`, all Radix-based):
`button`, `card`, `badge`, `switch`, `tabs`, `popover`, `select`,
`dropdown-menu`, `expander`, `expandable-text`, `markdown`.
**No `dialog`/`modal` wrapper component exists yet**, even though
`@radix-ui/react-dialog` is already a dependency — every current use of
Radix primitives is for lightweight popovers/menus, not full modals.

---

## 2. Current Layout

Single fixed three-zone shell, not the sidebar+workspace+context-panel
layout the request describes:

- **Header** (`AppShell.tsx`): product mark, top nav, theme toggle, one
  gear icon. No persistent left sidebar — conversation history lives
  behind that gear icon in `HistoryDrawer.tsx`, hidden by default,
  `md:pr-96`-reserved space only ≥`md` breakpoint.
- **Main**: the routed page, full width when the drawer is closed.
- **HistoryDrawer**: a *right-side* drawer (not left), combining chat
  history **and** all app settings in one panel — there is no separate
  "context panel" for schema/sources/query-plan; those render inline
  inside each `TurnCard` instead, as collapsible sections.

No resizable panels, no persistent open/closed sidebar state confirmed
(`settingsStore.collapsedSections` persists per-turn collapse state, not
a top-level sidebar-open flag). Mobile behavior is Tailwind-responsive at
the component level (`sm:`/`md:` prefixes spot-checked in `AppShell.tsx`,
`KnowledgeSources.tsx`, `MediaSearchResultCard.tsx`) but there is no
dedicated mobile drawer/hamburger nav — the same `HistoryDrawer` is used
at every viewport width.

---

## 3. Current Design Inconsistencies

- **Right-side combined history+settings drawer** does two unrelated jobs
  (conversation history navigation *and* global app settings) behind one
  icon — the request's Phase 3/4 model expects these as two separate
  concerns (left sidebar for history, a settings surface elsewhere).
- **No code syntax highlighting inside markdown-rendered answers.**
  `react-markdown` has no `components={{code: ...}}` override — a fenced
  code block in an assistant's *explanation* text (not the dedicated SQL
  box) renders as plain monospace text, no highlighting. The generated
  SQL itself *is* well-handled (CodeMirror, `@codemirror/lang-sql`, line
  numbers) — the inconsistency is between that box and any code appearing
  inside prose.
- **No copy button on the SQL `SqlEditor` itself** (a `CopyAnswerButton`
  exists for the answer text, but no equivalent was found wired into the
  SQL box specifically).
- **Citations show filename only** — no date/snippet/excerpt, even though
  the request wants a matching-excerpt preview in search results and
  richer citation display generally.
- **No true streaming, but a "thinking" affordance implies progressive
  output.** `askQuestion` (`src/lib/api.ts`) is one `await fetch(...).then(r
  => r.json())` call; `TimingBadge.tsx` shows a live-ticking "Thinking
  Ns…" counter for the whole wait, then the full answer appears at once.
  This is an honest, non-misleading pattern today (no fake token-by-token
  animation over a full response) but doesn't match the "streaming
  response state / stop-generation button" the request describes — that
  would require backend changes (see §9), not just a frontend rewrite.
- **Design tokens cover color/radius/font but not spacing or type scale**
  (see §7) — component-level spacing is ad hoc Tailwind utility values
  rather than drawn from a shared scale.

---

## 4. Existing Reusable Components

Confirmed real and in active use (not aspirational): `Button`, `Card`,
`Badge`, `Switch`, `Tabs`, `Popover`, `Select`, `DropdownMenu`, `Expander`,
`ExpandableText`, `Markdown` — all thin wrappers around Radix primitives
+ `class-variance-authority`/`tailwind-merge` for variants, a solid,
idiomatic foundation to build on rather than replace.

Chat-specific composed components worth reusing as-is or extending:
`TurnCard`, `ChatMessage`, `SourcesUsedPanel`/`SourceAnswerCard`,
`TimingBadge`, `RetryTimeline`, `SchemaContextPanel`, `QueryPlanPanel`,
`GoldenFeedbackWidget`.

---

## 5. Existing State Management

Zustand, 4 stores, no Redux/Context-heavy pattern:

- **`chatStore.ts`** — `queryHistory` (current conversation's turns),
  `conversations: Record<string, ConversationSummary>`,
  `activeConversationId`, `pendingQuestion`, `nlQuestionCache` (a `Map`),
  `goldenFeedbackGiven` (a `Set`). **Genuinely server-backed for
  locally-authenticated users**: `hydrateHistoryFromServer()` calls
  `GET /conversations` and is triggered by an effect in `AuthGate.tsx`
  keyed on the local-auth user id; `loadConversation()` lazily calls
  `GET /conversations/{id}/messages` the first time a conversation is
  opened; rename/delete call `PATCH`/`DELETE /conversations/{id}`. This
  corrects an earlier, now-stale characterization — server-side history
  is read back into the UI, not just written.
- **`settingsStore.ts`** — theme/accent/font/language/collapsed-sections/
  voice-toggle, persisted to `localStorage` (correctly scoped to
  UI-preference-only state, per the request's own guidance).
- **`authStore.ts`** (OIDC) / **`localAuthStore.ts`** (self-hosted
  accounts) — session/identity state.

`clearHistory()` (chatStore) resets every private field on logout/user
switch and is purely local — never calls a delete endpoint. A fresh login
re-triggers `hydrateHistoryFromServer()` via the same effect — no full
page reload required.

---

## 6. Existing API Contracts

- **`src/lib/api.ts`**: one `request<T>()` fetch wrapper — injects the
  bearer token, throws a typed `ApiError` on a non-2xx response, returns
  parsed JSON. `askQuestion` calls `POST /ask` this way.
- **`src/lib/identityApi.ts`**: `listConversations`, `getConversation`,
  `listMessages`, `searchChatHistory` (`GET /chat/search`),
  `renameConversationOnServer`, `deleteConversationOnServer`, and
  `archiveConversation` (exported but **never called from any
  component** — the backend `PATCH .../{archived:true}` route has no UI
  caller today).
- **No `AbortController`/request cancellation anywhere in `frontend/src`.**
  A user navigating away or firing a second question mid-request cannot
  cancel the first — a real gap for the "stop generation" requirement.
- **No streaming transport** (no `EventSource`, no `ReadableStream`
  consumption, no WebSocket) — `POST /ask` is request/response only,
  confirmed on both the client and by `api/schemas.py`'s `AskRequest`/
  `AskResponse` shapes.

---

## 7. Existing Design Tokens

`src/index.css` — CSS custom properties, theme switched via
`document.documentElement.dataset.theme` (a `data-theme` attribute), with
Tailwind's `dark:` variant remapped to match the same attribute
(`@custom-variant dark (&:where([data-theme='dark'] *))`) so both
mechanisms point at one source of truth.

**Present**: `--background`, `--foreground`, `--card`, `--border`,
`--muted`, `--sidebar`, `--input`, `--header`, `--accent`/
`--accent-foreground`/`--accent-soft`, `--success`/`--warning`/`--danger`,
`--radius` (+ derived `-lg/-md/-sm`), `--font-sans`/`--font-mono`,
`--shadow-color`.

**Missing** (relevant to the request's Phase 2 token list): an explicit
z-index layer scale, transition-duration tokens, a dedicated
`--user-message`/`--assistant-message` pair (message styling today is
ad hoc per-component, not token-driven), a dedicated `--code-block`
surface token, a dedicated `--sql-result` surface token, and a focus-ring
color token distinct from `--accent`. **Correction to an earlier draft of
this audit**: a spacing scale and reduced-motion handling are *not*
actually missing — Tailwind v4's built-in `--spacing`-derived utility
scale already serves as the spacing system (no separate hand-authored
token list needed), and `index.css:150-163` already has a global
`@media (prefers-reduced-motion: reduce)` override applied app-wide. A
type-scale is likewise already covered by Tailwind's default `text-xs`…
`text-4xl` utilities; adding a parallel CSS-variable type scale on top
would be pure duplication, not a real gap.

---

## 8. Existing UI Limitations (Summary)

1. No streaming — full-response-only, with a "thinking" spinner.
2. No request cancellation (no AbortController anywhere).
3. **Zero frontend tests, no test runner configured.** `package.json` has
   `dev`/`build`/`lint`/`preview` scripts only; no `vitest`/`jest`/
   `@testing-library/*` dependency exists at all.
4. No image/file attachment capability anywhere — frontend or backend.
   `ChatInput.tsx` is textarea+mic+send only; `AskRequest` has no file
   field; the only `UploadFile` backend routes are PDF documents
   (`api/documents.py`) and voice audio (`api/voice.py`).
5. Chart rendering client-side supports only bar/line regardless of
   backend intent (pre-existing finding, still true).
6. No code syntax highlighting for fenced code blocks inside markdown
   prose (SQL shown separately via CodeMirror is fine).
7. No spacing/type-scale design tokens — ad hoc Tailwind values.
8. Accessibility is "partial": native semantic elements are used
   consistently (`<nav>`, `<header>`, `<main>`, `<button>`), and
   icon-only buttons largely have `aria-label`/`title`, but there is no
   global live-region for streaming/async announcements, no confirmed
   `aria-expanded` on every collapsible, and the main chat textarea has
   no explicit `aria-label` (relies on placeholder text alone).
9. No toast/notification system — errors render as local inline colored
   text per-component; consistent in spirit but not a shared component.
10. Archive is a backend-only capability with no UI entry point.
11. History and settings are combined behind one icon/drawer rather than
    being two clearly separated surfaces.

---

## 9. Proposed Component Architecture (Adapted, Not Invented)

Reuse everything in §4 unmodified; the additions below map directly onto
gaps found above, not a rewrite:

```
src/components/
  layout/
    AppShell.tsx            (existing — evolve: split history vs. settings)
    Sidebar.tsx              NEW — left conversation-history rail (replaces
                              HistoryDrawer's history half; settings half
                              becomes its own surface)
    MobileNav.tsx             NEW — hamburger + drawer for small viewports
    HistorySettingsSection.tsx (existing — becomes settings-only)
  chat/
    ChatInput.tsx            (existing — extend with attach button, stop
                              button, AbortController wiring)
    TurnCard.tsx / ChatMessage.tsx / PendingTurn.tsx  (existing, reused)
    ConversationSearch.tsx   NEW — extracted from HistoryDrawer's inline
                              search box, so it's independently testable
  sql/
    SqlEditor.tsx            (existing — add copy button)
    ResultsTable.tsx / ResultChart.tsx (existing, reused)
  image/                     NEW — see §10; entirely additive
    ImageUploader.tsx, ImagePreview.tsx, ImageEditor.tsx, ImageViewer.tsx
  ui/
    dialog.tsx                NEW — thin wrapper around the already-installed
                               @radix-ui/react-dialog, for ImageEditor/
                               ImageViewer modals and any future confirm
                               dialogs
    toast.tsx                 NEW — shared error/success notification,
                               replacing ad hoc inline error text
```

---

## 10. Proposed Image-Editing Architecture (Frontend-Only Today)

**Confirmed: zero backend support exists for chat image attachments or
AI-guided image editing.** `AskRequest` has no file field; no endpoint
exists to accept, store, or AI-edit an uploaded chat image (distinct from
PDF document upload and from `media_gen`'s text-to-image *generation*,
which takes a text prompt, not an uploaded image).

Per the request's own explicit instruction ("if the backend does not yet
support image editing, implement the complete frontend editor and a clean
adapter interface, then document the backend contract required — do not
fake successful AI editing"), the realistic scope is:

- A fully working **local** editor (crop/rotate/flip/zoom/pan/draw/
  annotate/undo-redo) that never claims AI involvement for anything it
  does client-side.
- An `ImageEditAdapter` interface with one implementation
  (`LocalCanvasAdapter`, does real work) and one clearly-labeled stub
  (`AiGuidedEditAdapter`, throws `NotImplementedError` / renders a
  "requires backend support" message) — so AI-guided actions ("remove
  the selected object," "replace the sky") are visibly present in the
  UI as future capability, never silently faked as working.
- **Canvas library choice**: no canvas/drawing library exists in
  `package.json` today. Of the options the request names, **Konva +
  react-konva** is the best fit — React-idiomatic (unlike raw Canvas API,
  which would mean hand-rolling hit-testing/layering/undo-redo from
  scratch), lighter and more scriptable than Fabric.js for this specific
  feature set, and far less opinionated/heavy than tldraw (a full
  whiteboard app, overkill for "edit one photo"). This is a real new
  dependency, not something already in the repo — flagged for explicit
  confirmation before adding, per "do not introduce a library without
  justification."
- **Backend contract to document** (not build, unless separately
  requested): an endpoint shaped like `POST /media/edit` accepting the
  original image + a mask + a natural-language instruction, returning a
  new image — mirroring the existing `media_gen` image-generation
  pattern (human-approval gate, cost ceiling, SSRF-hardened storage) — a
  substantial backend feature in its own right, not a frontend task.

---

## 11. Accessibility Issues (Confirmed)

- Main composer `<textarea>` has no explicit `aria-label`.
- No global `aria-live="polite"` region for async state changes (a new
  answer arriving, a search result count changing).
- No confirmed focus-trap in any overlay (no `dialog.tsx` wrapper exists
  yet to standardize this).
- No `prefers-reduced-motion` handling found anywhere.
- Icon-only buttons are inconsistently labeled — most spot-checked ones
  have `aria-label`, but this hasn't been audited exhaustively across
  every component.

## 12. Performance Issues (Confirmed)

- No list virtualization — `HistoryDrawer`'s conversation list renders
  every hydrated conversation (`GET /conversations?limit=100`) without
  windowing; fine at 100, a real problem if that limit grows.
- No `AbortController` anywhere — a superseded in-flight request (e.g.
  rapid search typing before debounce, or navigating away mid-`/ask`)
  keeps running and can still update state after the component that
  cared is gone.
- No frontend bundle-size or render-performance measurement tooling
  configured (no bundle analyzer, no React DevTools profiling artifacts
  found in the repo).

---

## Summary

The existing frontend is a genuinely solid, Radix+Tailwind+Zustand
foundation with real server-backed conversation history and search
already wired end-to-end for local-auth users — better than the request's
own framing assumes. The real gaps are specific and scoped: no streaming
(a backend+frontend change), zero test coverage, no image/attachment
capability at any layer, incomplete design tokens, partial accessibility,
and a settings/history UI that combines two concerns behind one control.
Phase 2 onward should target these concretely rather than a ground-up
rewrite.
