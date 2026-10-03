# Navigation and action ownership

Companion to [`docs/ui-production-audit.md`](ui-production-audit.md) (what
was found duplicated and why) — this document is the resulting contract:
**one action, one owner, one location**, so a future change never has to
guess which of two copies to update, or accidentally leaves one stale.

## Ownership table

| Action | Single owner/location | Behavior |
|---|---|---|
| New chat | Sidebar (`Sidebar.tsx`) | `chatStore.startNewChat()` — resets to a fresh, unsaved conversation; persisted to the server only once a question is actually asked |
| Chat-history search | Sidebar (`ConversationSearch`/`useChatSearch`) | Debounced `GET /chat/search`, scoped server-side to the authenticated user; falls back to client-side title filtering only when no local-auth session exists |
| Sidebar collapse/expand | Header (`SidebarToggle`, one instance) | Toggles `settingsStore.sidebarCollapsed` — desktop-only (`>=lg`); below `lg` the sidebar is always the `MobileNav` drawer instead, which this control doesn't affect |
| Mobile navigation drawer | Header (`MobileNav`, `<lg` only) | Opens the same `Sidebar` component as an off-canvas drawer; closes itself after New Chat / selecting a conversation (`onNavigate`) |
| Settings/customization | User profile menu (`UserMenu` → "Settings") | Opens the single `SettingsDialog` instance (owned by `AppShell.tsx`) → unmodified `HistorySettingsSection` (appearance, connection status, schema browser, AI-insight/voice toggles) |
| Theme | User profile menu (quick `ThemeToggle` row) **and** inside Settings' Appearance section | Documented, intentional shortcut — see "Theme: the one deliberate exception" below, not a duplicate |
| Logout | User profile menu (`UserMenu` → "Sign out") | Calls local-auth logout if a local session exists, else OIDC sign-out if configured; never deletes server-side history (soft state only, see `docs/chat-history-ui.md`) |
| Profile / account info | User profile menu (`UserMenu` header row) | Display name + email, read from `useLocalAuthStore`; absent (no header row) when no local account is signed in |
| SQL context (schema, plan, validation, results) | Inline, per conversation turn (`TurnCard.tsx`'s collapsible sections) | **Not** a persistent right-side panel — see "Why no separate context panel" below |
| Image editing | Image attachment chip / message actions only | Opens `ImageEditor` (lazy-loaded) for that specific attachment; never appears in `SettingsDialog` or any global menu |
| Page navigation (Chat / Knowledge Sources / Media Search) | Header nav tabs | Plain client-side routes (`react-router`), unrelated to account/settings actions, kept in the header since it's genuine cross-page navigation, not a duplicated setting |
| Database Onboarding (Prompt 26) | Header nav tab, **gated on role** (`admin`/`analyst`, read from `useLocalAuthStore`) | The one nav tab that isn't always visible — unlike every other entry in this table, it has no useful read-only purpose for an account with neither role, so it's omitted rather than shown leading nowhere. UX-only gating; the real enforcement is server-side (`api/onboarding.py`'s own `ONBOARDING_MANAGE`/`ONBOARDING_REVIEW` checks) |

## Theme: the one deliberate exception, and why

`ThemeToggle` (the same component, reading/writing the same
`settingsStore.themeMode`) appears in two places:

1. **`UserMenu`'s quick row** — a fast, no-navigation way to try light/
   dark/system while doing something else, rendered as plain content
   (not a `DropdownMenuItem`) specifically so clicking a theme option
   doesn't close the menu — a user can compare more than one theme in a
   single open.
2. **`SettingsDialog` → Appearance section** — the full customization
   surface, alongside accent color, font, and language, which don't have
   their own header shortcuts.

This is the *same component and the same store*, not two competing
implementations — changing the theme from either place is instantly
reflected in the other. It is kept because theme is the one setting
common enough to want a zero-navigation shortcut for, matching this
document's own governing rule ("place the same action in two locations
only when the second is clearly a shortcut and documented here").
Everything else that used to be reachable from two places (Settings
itself, Logout) was consolidated to exactly one.

## Why no separate right-side context panel

The originating request's suggested architecture includes an optional
right-side "context panel" for schema/SQL/retrieval details. This
repository's existing chat rendering already presents that information —
generated SQL, executed SQL, validation/cost notices, retrieved schema,
query plan, sources used — as **collapsible sections inline within each
conversation turn** (`TurnCard.tsx` composing `SchemaContextPanel`,
`QueryPlanPanel`, `SourcesUsedPanel`, `SqlEditor`, `ResultsTable`), not
expanded by default. Introducing a second, persistent panel showing the
same data would either duplicate it in two places at once or require
restructuring already-working, previously-reviewed chat components for no
reported problem — none of the duplication/clutter complaints that
motivated this pass were about SQL/context presentation. Kept as-is,
documented here as a deliberate adaptation rather than a silently-skipped
requirement.

## Single source of truth for sidebar state

- **`settingsStore.sidebarCollapsed`** (persisted to `localStorage`,
  `tsql-settings` key alongside theme/accent/font — a boolean UI
  preference, never chat content) is the *only* representation of "is the
  desktop rail expanded or collapsed." `SidebarToggle` and `Sidebar`
  (via its `collapsed` prop, read from this same store in `AppShell.tsx`)
  both derive from it — there is no second boolean anywhere that could
  drift out of sync.
- **`MobileNav`'s local `open` state** is a *different* concern (is the
  off-canvas overlay currently showing) — ephemeral, always starts closed
  on load, and deliberately never persisted or unified with the collapse
  preference above. Conflating the two would mean a collapsed desktop
  preference either leaking into "the mobile drawer opens already
  collapsed" (nonsensical — the drawer is a full-width overlay with no
  rail state of its own) or the reverse. Kept separate on purpose.

## Verification

Real headless-Chromium screenshots (Playwright, launched against the
actual Vite dev server, auth mocked via request interception since no test
credentials were available in this environment) were taken and visually
reviewed at:

- **1440×900 (desktop)** — sidebar expanded (default), sidebar collapsed
  (after clicking `SidebarToggle`), and the `UserMenu` open (showing
  display name, email, Settings, Theme, Sign out — all present exactly
  once).
- **820×1024 (tablet)** — confirmed the off-canvas drawer pattern, no
  horizontal overflow.
- **390×844 (mobile)** — confirmed the hamburger → drawer flow, composer
  remains visible and usable, no overflow.
- **Dark mode** — switched live via the `UserMenu`'s theme row, confirmed
  correct contrast and no unstyled flash.

Zero browser console errors were captured in any of the above. See
`docs/ui-production-audit.md` §6 for the summary and
`docs/ui-design-system.md` for the token/visual system these screens draw
from.
