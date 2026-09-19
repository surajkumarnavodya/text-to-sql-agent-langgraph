# UI Production Audit

**Date:** 2026-09-18. This is a follow-up pass on top of the 2026 UI
redesign documented in [`docs/frontend-ui-audit.md`](frontend-ui-audit.md)
(the ground-up layout rebuild: left sidebar, search, image attachments) —
that pass fixed the *big* structural gaps but introduced a *new*, narrower
problem this audit exists to fix: real, verified duplication of
account-level controls, and no way to collapse the sidebar at all. Every
claim below was verified by reading the actual component source and
handlers (not assumed from visual similarity) and, where noted,
cross-checked against real headless-browser screenshots.

---

## 1. Duplicated controls found (verified by reading handlers, not just markup)

### Settings/customization button — **two separate implementations**

| Location | File | Before this pass |
|---|---|---|
| Header, gear icon | `AppShell.tsx` | `onClick={() => setIsSettingsOpen(true)}`, its own local `isSettingsOpen` state, mounting its **own** `<SettingsDialog>` instance |
| Sidebar footer, gear icon | `Sidebar.tsx` | `onClick={() => setIsSettingsOpen(true)}`, a **second**, independent local `isSettingsOpen` state, mounting a **second** `<SettingsDialog>` instance |

Not a cosmetic duplicate — these were **two independently-mounted
`SettingsDialog` components with two independent open/closed booleans**.
Opening one had zero effect on the other; in principle both could be open
at once (impossible to see in practice since the sidebar footer's gear
sits behind the header's, but the state genuinely existed twice). Both
called the identical `HistorySettingsSection` content, so the dialogs
themselves would have looked identical — the duplication was in the
*wiring*, which is exactly the kind of bug that "looks harmless" until a
future change to one copy silently doesn't apply to the other.

### Logout button — **two copies of the same branching logic, copy-pasted**

| Location | File |
|---|---|
| Header, icon button | `AppShell.tsx` |
| Sidebar footer, icon button | `Sidebar.tsx` |

Both contained the exact same conditional: `localAuthStatus ===
'authenticated' ? localSignOut() : (isOidcConfigured && authStatus ===
'authenticated' && signOut())`, independently written out in full in both
files. Functionally identical (verified: both call the same store
actions), but two copies of non-trivial branching logic is a real
maintenance hazard — a future change to the local/OIDC precedence order
would need to be made twice, and nothing would catch a session where only
one copy got updated.

### Theme toggle — one real instance plus one already-justified shortcut

`ThemeToggle` appeared in the header (`AppShell.tsx`) **and** inside
`HistorySettingsSection`'s Appearance section (reached via either gear
button above). This is the one duplication that's arguably fine as a
"quick access + full settings" pattern — but it existed with **no
documentation saying so**, which is indistinguishable from an accidental
duplicate to a future reader. Fixed in this pass by making a shortcut
inside the user menu (not the bare header) and documenting the decision
explicitly — see §7.

### No sidebar collapse at all

`Sidebar`'s only state was `hidden ... lg:flex` — a **breakpoint-driven
show/hide**, not a user-controlled collapse. There was no
`sidebarCollapsed` concept anywhere in the codebase, no icon-rail mode, and
therefore nothing to persist. This matches the user-reported symptom
exactly ("the left sidebar cannot be collapsed").

### Not duplicated (verified, listed so they aren't mistakenly "fixed" twice)

- **New Chat** — one implementation (`Sidebar.tsx`), reused unmodified
  inside `MobileNav`'s drawer. Not duplicated.
- **Search** — one implementation (`ConversationSearch`/`useChatSearch`),
  same reuse pattern. Not duplicated.
- **Sidebar-toggle/hamburger** — `MobileNav`'s hamburger (opens the
  off-canvas drawer, `<lg`) and the new desktop `SidebarToggle` (collapses
  the rail, `>=lg`) are two *different* actions for two different
  interaction patterns (an overlay vs. a rail), each visible only at its
  own breakpoint, never both at once — not a duplicate, see §2's
  "one action, one owner" test applied to this case in
  `docs/navigation-and-actions.md`.
- **Image-edit button** — only ever rendered on an attached image's own
  `AttachmentChip`/message actions, never in a global settings area.
  Confirmed via `grep` — no second call site exists.
- **Menu button** — one hamburger (`MobileNav`), one user-avatar trigger
  (`UserMenu`) — different actions (navigation vs. account), not a
  duplicate pair.

---

## 2. Every location settings/customization appeared (before → after)

| Before | After |
|---|---|
| Header gear icon → own `SettingsDialog` instance | **Removed** |
| Sidebar footer gear icon → own `SettingsDialog` instance | **Removed** |
| — | **`UserMenu`'s "Settings" item** (header, one instance) → the single, still-unmodified `SettingsDialog`, now mounted exactly once in `AppShell.tsx` |

## 3. Every location logout appeared (before → after)

| Before | After |
|---|---|
| Header icon button, own branching logic | **Removed** |
| Sidebar footer icon button, duplicated branching logic | **Removed** |
| — | **`UserMenu`'s "Sign out" item** (header, one instance), one shared `handleSignOut` closure |

## 4. Every location profile/user info appeared (before → after)

| Before | After |
|---|---|
| Sidebar footer: plain, non-interactive `<span>` with the display name, no email, no menu | **Removed** |
| — | **`UserMenu` trigger**: avatar-style initials badge; opening it shows display name + email, per `docs/navigation-and-actions.md` |

## 5. Sidebar-related controls (before → after)

| Control | Before | After |
|---|---|---|
| New Chat | Sidebar, full-width button | Unchanged |
| Search | Sidebar, below New Chat | Unchanged |
| Conversation list | Sidebar, scrollable | Unchanged |
| Collapse/expand | **Did not exist** | `SidebarToggle`, header, single instance, toggles `settingsStore.sidebarCollapsed` |
| Settings/Logout/profile | Sidebar footer (3 controls + a hidden 2nd dialog) | **Removed from Sidebar entirely** — `Sidebar.tsx` now owns exactly one concern, conversation-history navigation |

---

## 6. Current mobile/desktop/responsive behavior (verified via headless-Chromium screenshots)

A dev server was actually launched and screenshotted (Playwright,
route-mocked auth since no test credentials were available) at 1440px
(desktop), 820px (tablet), and 390px (mobile) — see
`docs/navigation-and-actions.md`'s "Verification" section for exactly what
was checked and the result. Summary:

- **Desktop (≥1024px / `lg`)**: persistent left sidebar, expandable/
  collapsible via the header's single toggle; collapsing shows a narrow
  icon rail (New Chat only) and the main chat area visibly reflows to use
  the freed width — no layout jump, confirmed by screenshot.
- **Tablet (820px)**: below `lg`, falls back to the same off-canvas drawer
  pattern as mobile (one of the two behaviors this kind of request
  explicitly allows for tablet) — confirmed no horizontal overflow.
- **Mobile (390px)**: header collapses to a single hamburger + brand +
  avatar; the drawer opens the *same* `Sidebar` component at full width;
  the composer remains visible and usable at the bottom; no double
  scrollbars or overflow observed.
- **Dark mode**: switched live from the new `UserMenu` theme row,
  confirmed via screenshot — correct contrast, no unstyled flash.
- **Zero browser console errors** captured across all three breakpoints
  during this verification pass.

## 7. Accessibility problems found and fixed in this pass

- The header's gear/logout icon buttons already had `aria-label`/`title`
  (fine, unchanged). The **new** `UserMenu` and `SidebarToggle` continue
  that same convention (native `title` + `aria-label` is this codebase's
  established tooltip mechanism — no separate Tooltip component exists or
  was introduced, see `docs/ui-design-system.md`).
- `SidebarToggle` carries `aria-expanded` (reflects collapsed/expanded)
  and `aria-controls` (points at the `<aside id="app-sidebar">` it
  operates on) — neither existed before, since no toggle existed.
- `UserMenu` uses Radix's `DropdownMenu` primitive (already a dependency,
  already used elsewhere in this codebase) rather than a hand-rolled
  popover — real focus management, `Escape`-to-close, and
  click-outside-to-close come from Radix, not reimplemented. Verified in
  `UserMenu.test.tsx` (`Escape` closes it) and by real keyboard-driven
  interaction during the screenshot pass.
- No keyboard trap was found or introduced — `Tab` reaches every control
  in document order (header → sidebar → composer), same as before.

## 8. Inconsistent spacing/typography/icon/color/variant issues found

Nothing new introduced by this pass beyond what
[`docs/ui-design-system.md`](ui-design-system.md) already documents and
fixed (message/code/SQL-surface tokens, a focus-ring token, z-index/
duration scales). This pass reused the **exact same** `Button` component
and `variant`/`size` props already established (`ghost`/`icon` for every
icon-only header control, `secondary`/`sm` for New Chat) — no new button
variant, icon size, or spacing scale was introduced for `UserMenu`/
`SidebarToggle`, deliberately, to avoid adding a second visual language
next to the existing one.

## 9. Unnecessary/confusing UI elements removed

- Two independently-stateful `SettingsDialog` mounts → one.
- Two logout buttons with copy-pasted branching → one shared handler.
- A non-interactive display-name `<span>` that looked like it might be
  clickable (it wasn't) → replaced by a real, labeled, keyboard-operable
  `UserMenu` trigger.

---

## 10. Recommended final information architecture

See [`docs/navigation-and-actions.md`](navigation-and-actions.md) for the
full ownership table this section summarizes:

```
AppShell
├── Header
│   ├── MobileNav (hamburger, <lg only — opens the drawer)
│   ├── SidebarToggle (>=lg only — collapses/expands the rail)
│   ├── Brand + env indicator
│   ├── Page nav (Chat / Knowledge Sources / Media Search)
│   └── UserMenu (avatar -> display name/email, Settings, Theme, Sign out)
├── Sidebar (persistent >=lg, or MobileNav's drawer content <lg)
│   ├── New Chat
│   ├── ConversationSearch
│   └── ConversationList
├── SettingsDialog (single instance, owned by AppShell, opened only via UserMenu)
└── MainWorkspace (routed page content — Chat.tsx, etc., unchanged by this pass)
```

**Right context panel**: this repository's existing architecture already
renders SQL/schema/retrieval context as collapsible sections *inline*,
per conversation turn (`TurnCard.tsx` → `SchemaContextPanel`,
`QueryPlanPanel`, `SourcesUsedPanel`, `SqlEditor`, `ResultsTable`) rather
than in a separate persistent side panel. This pass deliberately did
**not** introduce a new right-side panel to house the same information a
second time — doing so would either duplicate that content in two places
or require a much larger, riskier rearchitecture of the chat-rendering
components for no reported problem (the user's actual complaints were
about the *left* side and account controls, not about SQL/context
presentation). Documented as a deliberate adaptation, not an oversight —
see `docs/navigation-and-actions.md`'s "SQL context" row.

## 11. Components merged, removed, or reused

| Component | Disposition |
|---|---|
| `UserMenu.tsx` | **New** — consolidates what was 2×Settings + 2×Logout + 1×inert name label into one component |
| `SidebarToggle.tsx` | **New** — the sidebar-collapse control, single instance |
| `AppShell.tsx` | **Modified** — header simplified from 4 loose controls (logout, theme, gear, nothing wired together) to 2 composed ones (`SidebarToggle`, `UserMenu`) |
| `Sidebar.tsx` | **Modified** — footer (settings/logout/name) removed entirely; gained `collapsed` prop and icon-rail render path |
| `HistorySettingsSection.tsx` | **Unchanged** (content) — only its doc comment, describing where it's reachable from |
| `MobileNav.tsx` | **Unchanged** — already reused `Sidebar` correctly; unaffected by the collapse feature (mobile always renders expanded) |
| `dropdown-menu.tsx` | **Extended** — added `DropdownMenuSeparator`/`DropdownMenuLabel` (Radix primitives this file didn't wrap yet), needed by `UserMenu` |
