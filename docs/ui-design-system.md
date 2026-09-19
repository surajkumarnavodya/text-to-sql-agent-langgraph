# Frontend Design System

Extends the existing Tailwind v4 + CSS-custom-property token system
(`frontend/src/index.css`) — no new styling framework was introduced, per
the "continue using the repository's existing approach" constraint. This
document covers what changed in this pass; see
`docs/frontend-ui-audit.md` §7 for the full before/after token inventory.

## Theming

Unchanged mechanism, still the source of truth: `data-theme` attribute on
`<html>` (`src/lib/theme.ts`), with Tailwind's `dark:` variant remapped to
match it (`@custom-variant dark (&:where([data-theme='dark'] *))`) so both
mechanisms point at one switch. Accent color and font are independently
user-configurable CSS variables on top of light/dark.

## Tokens added this pass

All additive — nothing existing was renamed or removed, so no consumer
broke.

| Token | Purpose |
|---|---|
| `--focus-ring` | Distinct semantic name for the keyboard-focus ring color (currently mirrors `--accent`, but themeable independently later). Adopted in `Button`, `Dialog`'s close button, `ConversationSearch`, `ConversationListItem`'s rename input. |
| `--user-message-bg` / `--user-message-foreground` | Named surface for the user's side of a conversation turn. |
| `--assistant-message-bg` / `--assistant-message-foreground` | Named surface for the assistant's side. |
| `--code-block-bg` / `--code-block-foreground` | Named surface for code/SQL content, distinct from generic `--muted`/`--card`. |
| `--sql-result-bg` | Named surface for the SQL result area. |
| `--z-dropdown` / `--z-drawer` / `--z-modal` / `--z-toast` | Named stacking tiers (30/40/50/60) so a new overlay picks the right layer without guessing against existing ad hoc z-index numbers. |
| `--duration-fast` / `--duration-base` / `--duration-slow` | Named transition speeds (100/150/250ms) instead of a bare value repeated per component. |

**Deliberately not added** (see the audit's correction): a separate
spacing scale and a separate type scale. Tailwind v4's own built-in
spacing/`text-*` utilities already provide a consistent scale — adding a
parallel CSS-variable version would be pure duplication, not a real gap.
Reduced-motion support also already existed
(`index.css`'s `@media (prefers-reduced-motion: reduce)` block, global) —
an earlier draft of the audit incorrectly flagged it as missing; corrected
before this pass began.

## New shared components (`components/ui/`)

- **`dialog.tsx`** — a thin wrapper around `@radix-ui/react-dialog`
  (already an existing dependency, previously unused as a standalone
  component). Real focus trapping, Escape-to-close, `aria-modal`, and
  scroll-lock, all from Radix — not hand-rolled.
- **`drawer.tsx`** — the same Radix Dialog primitive, positioned as a
  sliding side panel instead of a centered modal. Used by `MobileNav`.
- **`toast.tsx`** — `ToastProvider` + `useToast()`. Replaces the pattern
  of each component rolling its own inline colored error text.
  Accessible by construction: an error toast is `role="alert"`
  (interrupts a screen reader), a success/info toast is `role="status"`
  (announced without interrupting) — both individually dismissible, never
  relying on the auto-dismiss timer alone.
- **`copy-button.tsx`** — shared icon-button copy affordance (used by
  `SqlEditor`), separate from the existing markdown-aware
  `CopyAnswerButton` (which does a richer HTML+plain-text dual clipboard
  write appropriate for prose, not code).

**Note on adoption scope**: `Toast` is wired into the app root
(`App.tsx`) and available everywhere via `useToast()`, but existing
inline-error components (e.g. `TurnCard.tsx`'s per-turn status text) were
**not** retrofitted to use it in this pass — that would be a much larger,
separately-riskier refactor touching many already-working components.
New code paths added in this pass do use it where relevant.

## Layout change: Sidebar/Settings split

The single, combined right-side history+settings drawer
(`HistoryDrawer.tsx`, now removed) is replaced with:

- **`layout/Sidebar.tsx`** — a persistent left rail on `lg:`+ viewports,
  collapsible to an icon rail (see "Sidebar collapse" below) — New Chat,
  search, and conversation list only. Owns exactly one concern (history
  navigation); it has no footer of its own.
- **`layout/MobileNav.tsx`** — a hamburger trigger + slide-in drawer below
  `lg:`, rendering the *same* `Sidebar` component (not a second
  implementation, and always in its fully-expanded form regardless of the
  desktop collapse preference) via a `variant`-free `onNavigate` callback
  that closes the drawer after a selection.
- **`layout/UserMenu.tsx`** — the single, consolidated account-level menu
  (avatar/initials trigger → display name, email, Settings, Theme,
  Sign out), rendered once, in the header. Replaces what was previously
  two independent copies each of Settings and Sign out (one in the header,
  one in the Sidebar's own footer, each with its own state) — see
  `docs/ui-production-audit.md` for exactly what that duplication looked
  like and `docs/navigation-and-actions.md` for the resulting one-owner
  rule.
- **`layout/SidebarToggle.tsx`** — the sidebar collapse/expand control,
  rendered exactly once (header), not duplicated into the sidebar itself.
- **`layout/SettingsDialog.tsx`** — the same, unmodified
  `HistorySettingsSection` content, in one dialog instance owned by
  `AppShell.tsx`, opened only via `UserMenu`'s "Settings" item.

History logic (search debounce, rename/delete, hydrate-from-server) was
extracted out of the old drawer into `hooks/useChatSearch.ts` and three
presentational components (`ConversationSearch`, `ConversationList`,
`ConversationListItem`, `ConversationSearchResults`) — each independently
unit-tested (see `docs/frontend-ui-audit.md`'s proposed architecture,
now implemented).

## Sidebar collapse

`settingsStore.sidebarCollapsed` (persisted, `tsql-settings` localStorage
key alongside theme/accent/font — a non-sensitive UI preference, never
chat content) is the single source of truth for whether the desktop
(`>=lg`) rail is expanded (`w-72`) or collapsed to an icon rail (`w-14`).
`AppShell.tsx`'s `<aside>` transitions its `width` on change
(`transition-[width] duration-[var(--duration-base)]`, so it inherits the
same reduced-motion override every other transition in this app already
respects — see `index.css`'s global `prefers-reduced-motion` block).
Collapsed, `Sidebar` renders a narrow icon-only rail (New Chat only —
a list of bare conversation titles can't render meaningfully at icon
width, so the list and search are hidden rather than squeezed into
illegibility); expanding restores the full content exactly as it was,
since nothing about the underlying `chatStore` history state is affected
by the collapse preference. `MobileNav`'s drawer never collapses — see
`docs/navigation-and-actions.md`'s "single source of truth for sidebar
state" for why that's a deliberately separate, unpersisted concern from
this preference.

## Accessibility fixes in this pass

- Explicit `aria-label` on the chat composer `<textarea>` (previously
  relied on placeholder text alone).
- A visually-hidden `aria-live="polite"` region (`ChatLiveRegion.tsx`)
  announces exactly two moments — a question starting to process, and its
  answer arriving — never the live "Thinking Ns…" tick, which would
  otherwise re-announce every second.
- Search result count announced via `role="status"` +
  `aria-label` (`ConversationSearchResults.tsx`).
- `NavTab` (top nav) carries `aria-label`/`title` even when its visible
  text label collapses to icon-only below `sm:`.

## Known gaps, not addressed in this pass

- Existing components' inline error text was not migrated to the new
  `Toast` component (see "Note on adoption scope" above).
- No exhaustive accessibility audit was performed across every existing
  component — only the pieces touched or newly built in this pass were
  checked.

**Resolved by a later pass** (this note kept for history rather than
silently deleted): this section used to also flag that the header's
Settings gear and Sign out button were duplicated a second time in the
Sidebar's own footer, and that no sidebar-collapse feature existed. Both
are fixed — see `docs/ui-production-audit.md` and
`docs/navigation-and-actions.md`.
