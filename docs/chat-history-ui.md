# Chat History — Frontend UI

This covers the **UI layer** specifically (component architecture,
layout, what's wired to what). For the backend design (tables, API
routes, retention/soft-delete semantics), see the existing
`docs/chat-history-architecture.md` and `docs/chat-history-search.md` —
this document doesn't repeat that content, only what changed on the
frontend in this pass and how the pieces fit together.

## Corrected assumption, verified against real code

Before this pass, it was easy to assume (and an earlier draft of this
project's own audit initially did) that server-side conversation history
was "write-only" — persisted by `POST /ask`'s side effect but never read
back into the UI. **That's false.** The frontend genuinely calls the real
read APIs:

- `GET /conversations` — fetched on login/session-restore via an effect
  in `AuthGate.tsx` (`hydrateHistoryFromServer()`).
- `GET /conversations/{id}/messages` — fetched lazily the first time a
  conversation is opened (`chatStore.loadConversation`).
- `GET /chat/search` — powers real server-side search across the user's
  complete history, not just conversations already loaded in memory.

This is real, tested (indirectly, via `Sidebar.test.tsx` and the
extracted component tests below), and unchanged in this pass — the UI
work here reorganizes *where* this logic is presented, not whether it
works.

## Component architecture (current)

```
Sidebar.tsx                    -- history navigation ONLY; owns
  |                                rename-in-progress state and the
  |                                `collapsed` icon-rail render path
  ├── useChatSearch()          -- hook: debounced GET /chat/search,
  |                                falls back to client-side title filter
  |                                when server history isn't active
  ├── ConversationSearch.tsx   -- presentational search input
  ├── ConversationSearchResults.tsx  -- server search results list
  └── ConversationList.tsx     -- recency-grouped list (Today/Yesterday/…)
        └── ConversationListItem.tsx  -- one row: select/rename/delete
```

`Sidebar` itself is rendered twice — once persistently in `AppShell.tsx`
(desktop, `lg:`+, collapsible via the header's `SidebarToggle`), once
inside `MobileNav.tsx`'s slide-in `Drawer` (below `lg:`, always fully
expanded) — the *same* component both times, distinguished only by an
`onNavigate` callback the mobile drawer passes to close itself after a
selection, and an optional `collapsed` prop the mobile drawer never
passes. There is exactly one history-list implementation, not two to
keep in sync.

**Settings/Theme/Sign out do not live in `Sidebar` at all** — see
`docs/navigation-and-actions.md` for the single-owner `UserMenu` (header)
those actions moved to, and `docs/ui-production-audit.md` for the
duplicated Settings-dialog-plus-Sign-out-button pair (Sidebar footer +
header, each independently stateful) this replaced.

## What moved, what didn't

- **Moved (again, in a later pass)**: Settings/Theme/Sign out out of the
  Sidebar's own footer entirely — they now live only in the header's
  `UserMenu`. Previously (the pass this section originally described) they
  were reachable from *two* independent places (Sidebar footer, header
  gear icon), each mounting its own `SettingsDialog`; now there is one
  location and one dialog instance.
- **Unchanged**: `HistorySettingsSection.tsx`'s own content/logic —
  only where it's mounted (now `AppShell.tsx`, via `UserMenu`) changed.
- **Unchanged**: the underlying `chatStore.ts` actions
  (`hydrateHistoryFromServer`, `loadConversation`, `renameConversation`,
  `deleteConversation`) — nothing about server-side history reading/
  writing was touched by either UI pass.
- **Removed**: `HistoryDrawer.tsx` itself (fully superseded, confirmed no
  remaining imports before deletion).

## Testing

Each extracted piece has its own test file, using fixture conversations
built with the real `newHistoryEntry`/`ConversationSummary` types (not
loosely-typed mocks):

- `ConversationSearch.test.tsx` — typing, clear button, spinner-while-
  searching, Escape-to-clear.
- `ConversationList.test.tsx` — loading/empty states, recency grouping.
- `ConversationListItem.test.tsx` — select, rename (Enter commits, Escape
  cancels), loading label while active+loading.
- `ConversationSearchResults.test.tsx` — spinner, error (`role="alert"`),
  empty state, result selection.
- `Sidebar.test.tsx` — end-to-end through the real `chatStore` singleton
  (snapshotted/restored around each test): New Chat, client-side filter
  fallback (no server auth in the test), the collapsed icon-rail render
  path (New Chat only, search/list hidden, New Chat still functional), and
  a regression test asserting Sidebar renders **no** Settings/Sign-out/
  dialog markup of its own (that now lives solely in `UserMenu` — see
  `UserMenu.test.tsx`). No longer needs a `QueryClientProvider` wrapper,
  since `HistorySettingsSection`'s `@tanstack/react-query` hooks aren't
  reachable from this component anymore.
- `UserMenu.test.tsx` — display name/email rendering, Settings/Theme/
  Sign-out all present exactly once, Sign-out hidden when unauthenticated,
  `onOpenSettings` called exactly once, closes on `Escape`.
- `SidebarToggle.test.tsx` — `aria-expanded`/`aria-controls` reflect the
  shared `settingsStore.sidebarCollapsed` value in both directions, and
  clicking toggles that single store value.

## Known limitations (unchanged from before this pass — not introduced
by it)

- Universal server-side history only applies to locally-authenticated
  users (`useLocalAuthStore`'s `status === 'authenticated'`) — an
  OIDC-only or fully unauthenticated session still gets a purely
  in-memory, session-only conversation list, same as before.
- Archive (`PATCH /conversations/{id}` with `{archived: true}`) has a
  real backend route and a client function
  (`identityApi.ts::archiveConversation`) but **no UI entry point** —
  confirmed unused by any component before this pass, and not added in
  this pass either (out of scope; a reasonable, disclosed follow-up).
