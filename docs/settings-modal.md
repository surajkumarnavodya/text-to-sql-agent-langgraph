# Settings Modal

How Settings opens, renders, and closes. See
`docs/functional-ui-audit.md` §1 for the full investigation this document
summarizes (including the one real bug found and fixed).

## Implementation

`frontend/src/components/layout/SettingsDialog.tsx`, built on
`frontend/src/components/ui/dialog.tsx` (a thin wrapper around
`@radix-ui/react-dialog`). One instance in the whole app, owned by
`AppShell.tsx`, opened only via the header's `UserMenu` → "Settings" (see
`docs/navigation-and-actions.md` for why this is the single owner of that
action).

## Overlay and layering

- A full-screen, **fully opaque** `bg-[var(--modal-backdrop)]` backdrop
  (Radix `Dialog.Overlay`), rendered via a Portal so it's always above the
  app's own DOM tree regardless of where `SettingsDialog` is mounted. This
  was a translucent `bg-black/40` until 2026-09-19 — see
  `docs/settings-modal-visual-bug.md` for why that let background content
  bleed through, particularly (though not only) in dark mode.
- The modal panel sits above the backdrop, centered, `max-w-lg`, `max-h-
  [calc(100vh-2rem)]` with internal scroll — content never grows the page
  itself. Its background is `bg-[var(--modal-surface)]`, a token
  deliberately separate from `--card` (which carries a translucent "glass"
  alpha channel in dark mode, appropriate for message bubbles, wrong for a
  modal meant to fully occlude the page) — same 2026-09-19 fix.
- z-index: `--z-modal` (50 in `index.css`'s scale — `--z-dropdown: 30`,
  `--z-drawer: 40`, `--z-modal: 50`, `--z-toast: 60`), applied to both the
  overlay and the content so they always stack correctly above sidebar/
  header/chat/dropdowns.
- Background scroll is locked while open and interaction with content
  behind the backdrop is blocked — both are Radix `Dialog` defaults,
  unmodified. `AppShell.tsx`'s background wrapper also gets the native
  `inert` attribute while Settings is open (2026-09-19), removing it from
  the accessibility tree/tab order as well, on top of the opaque backdrop.

## Interaction

- **Escape** closes it (Radix default).
- **Backdrop click** closes it (Radix default, not overridden — this is
  the intentionally-supported behavior this task asked to confirm before
  keeping).
- **Close button** (top-right `X`) closes it.
- **Focus on open**: moves into the dialog (Radix `FocusScope`).
- **Focus on close**: returns to the header's account-menu button
  explicitly, via `SettingsDialog`'s `triggerRef` prop +
  `DialogContent`'s `onCloseAutoFocus` handler — **not** Radix's own
  automatic restoration, which doesn't work reliably here since Settings
  opens from a `DropdownMenuItem` that unmounts before the dialog mounts
  (see the audit's §1.3 for the full root-cause trace). This was the one
  real, fixed bug from this pass.

## Content

Renders the pre-existing `HistorySettingsSection` (Appearance/theme/
accent/font/language, per-database connection status + test/refresh
actions, discovered-tables list, AI-insight and voice-mode toggles) inside
the scrollable content region. No duplicate Theme/Logout/Profile controls
exist here — those are owned exclusively by `UserMenu`.

## Tests

`frontend/src/components/layout/SettingsDialog.test.tsx` — opens with a
labeled dialog and a real backdrop; closes on Escape and restores focus to
the trigger; closes via the close button and restores focus; moves focus
into the dialog on open.

`frontend/src/components/ui/dialog.test.tsx` and
`frontend/src/components/layout/AppShell.test.tsx` (both added 2026-09-19)
— the opaque-backdrop/opaque-panel/single-instance regression coverage and
the background-`inert`-while-open coverage described in
`docs/settings-modal-visual-bug.md`.

## Responsive behavior

Not restructured in this pass (no defect found) — `max-w-lg` with
viewport-relative `max-h` already reflows correctly at tablet/mobile
widths via Tailwind's default responsive container behavior; a dedicated
mobile full-screen variant was not built since the current settings
surface (one scrolling list, no category navigation) doesn't need one to
stay usable at small widths.
