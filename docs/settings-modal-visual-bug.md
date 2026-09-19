# Settings Modal — Background Visibility Bug (2026-09-19)

Root-cause writeup for the bug reported 2026-09-19: with the Settings
popup open in **dark mode**, the chat page behind it (SQL text, the
"Confirm and Run" button, the results table) remained visible both around
*and bled through* the modal panel itself. See
`docs/settings-modal.md`/`docs/functional-ui-audit.md` §1 for the prior
pass on this same modal (focus restoration) — that pass tested only light
mode and did not catch this, which is why this is a separate, later fix
rather than a contradiction of that audit's findings.

## Root cause

Two compounding issues, both in shared primitives, both dark-mode-specific
(or dark-mode-only, respectively):

### 1. The dialog panel's own background was translucent in dark mode

`frontend/src/components/ui/dialog.tsx`'s `DialogContent` painted the
modal panel with `bg-[var(--card)]`. `--card` is defined per-theme in
`frontend/src/index.css`:

```css
:root {
  --card: #ffffff;        /* light mode: fully opaque */
}
:root[data-theme='dark'] {
  --card: #15132485;      /* dark mode: RGB #151324 + alpha 0x85 (~52%) */
}
```

`#15132485` is an **8-digit hex color** — the trailing `85` is an alpha
channel, not part of the RGB value. In dark mode, `--card` is therefore
only ~52% opaque by design. This is intentional elsewhere in the app: the
file's own top comment describes the dark theme as "violet/near-black
**glass**," and `--assistant-message-bg: var(--card)` deliberately uses
this translucency for message bubbles, which are meant to layer over other
surfaces. The bug was **not** that `--card` is wrong — it's that
`dialog.tsx` reused a token designed for a layered "glass" surface as the
background of a modal panel, which instead needs to be the *only* thing
between the user and total occlusion of the page. Light mode's
`--card: #ffffff` has no alpha component, so this half of the bug never
appeared in light mode, which is exactly why the prior audit pass (light
mode only) didn't catch it.

### 2. The overlay/backdrop was only 40% opaque, in both themes

`DialogPrimitive.Overlay` used `bg-black/40` — Tailwind's shorthand for
`rgba(0, 0, 0, 0.4)`. Even with an opaque panel, 40% black over the page
still lets page content show through the backdrop area surrounding the
panel (and, combined with issue 1, through the panel too). This part of
the bug applies in both themes, but was harder to notice in light mode
because light-on-light backgrounds contrast less against a dark overlay
than dark-on-dark chat content does. `frontend/src/components/ui/drawer.tsx`
shared the identical `bg-black/40` overlay pattern (its own panel uses
`--sidebar`, which has no alpha channel in either theme, so only its
overlay — not its panel — had this issue).

## Why this wasn't caught earlier

`docs/functional-ui-audit.md`'s 2026-09-19 pass tested Settings live via
Playwright but only in the app's default (light) theme. The backdrop
mechanism itself (Radix `Dialog.Overlay`, focus trap, Escape-to-close,
z-index layering) was and remains correct — the audit's conclusions about
those are still accurate. The specific gap was theme coverage, not the
modal architecture.

## Fix

Two new CSS custom properties, deliberately **not** derived from `--card`,
added to `frontend/src/index.css`:

```css
:root {
  --modal-backdrop: #05050a;             /* fully opaque, both themes */
  --modal-surface: #ffffff;              /* light mode: same as --card (already opaque) */
  --modal-surface-foreground: var(--foreground);
}
:root[data-theme='dark'] {
  --modal-surface: #151324;              /* same hue as dark --card, alpha stripped */
}
```

`frontend/src/components/ui/dialog.tsx`: `DialogPrimitive.Overlay` now
uses `bg-[var(--modal-backdrop)]` (was `bg-black/40`);
`DialogPrimitive.Content` now uses `bg-[var(--modal-surface)]
text-[var(--modal-surface-foreground)]` (was `bg-[var(--card)]`).

`frontend/src/components/ui/drawer.tsx`: `DialogPrimitive.Overlay` now
uses the same `bg-[var(--modal-backdrop)]` token (was `bg-black/40`). Its
panel was already fully opaque (`--sidebar`) and is unchanged.

Both `frontend/src/components/image/ImageViewer.tsx` and
`ImageEditor.tsx` build on the same shared `Dialog`/`DialogContent`, so
they inherit this fix automatically — no per-component change was needed
there, and no other full-screen overlay exists in the app (`grep`-verified:
the only other `bg-black/` usage in `frontend/src` is a small opacity chip
on a media-search thumbnail, unrelated to modal occlusion).

`frontend/src/components/layout/AppShell.tsx`: the app-shell background
(sidebar + header + chat, i.e. everything except the portaled
`SettingsDialog` itself) now gets the native `inert` boolean attribute
while `isSettingsOpen` is true:

```tsx
<div className="flex h-full" inert={isSettingsOpen || undefined}>
```

This is additive to the opaque backdrop, not a substitute for it: the
backdrop handles *visual* occlusion, `inert` removes the background from
the accessibility tree and tab/pointer reachability while it's visually
hidden anyway. Because `SettingsDialog` renders through a Radix `Portal`
(teleported out of `AppShell`'s own DOM subtree), it is never a descendant
of the `inert` div and is unaffected by its own inert flag.

## Why not the `data-settings-open` / hide-the-app-shell approach

The prompt that reported this bug offered an alternative pattern: toggle
`opacity: 0`/`visibility: hidden` on the app shell itself via a
`data-settings-open` attribute, with an explicit caveat to avoid it if the
modal is nested inside the hidden shell. `SettingsDialog` already renders
through a `Portal` to `document.body`, so it was never nested inside
`AppShell`'s own DOM in the first place — but hiding the shell itself was
still not chosen, for a simpler reason: an opaque, full-viewport backdrop
already fully occludes the shell visually on its own, without needing to
also toggle the shell's own visibility. Doing both would be redundant and
adds a second state to keep in sync (a shell hidden via `opacity`/
`visibility` but not `inert`, or vice versa, is an easy way to reintroduce
exactly this class of bug later). `inert` alone covers the
non-visual half of the requirement (AT/keyboard reachability) that an
opaque backdrop doesn't.

## Verification

- `frontend/src/components/ui/dialog.test.tsx` — new: overlay never
  renders `bg-black`, always renders the `--modal-backdrop` token; panel
  never renders `bg-[var(--card)]`, always renders `--modal-surface`;
  exactly one dialog renders at a time.
- `frontend/src/components/ui/drawer.test.tsx` — new file: same overlay
  assertion for the drawer.
- `frontend/src/components/layout/AppShell.test.tsx` — new file: the
  background wrapper has no `inert` attribute before Settings opens, and
  has it once open.
- `frontend/src/components/layout/SettingsDialog.test.tsx` — pre-existing
  focus-trap/Escape/focus-restore tests, unaffected, still passing.
- Live dark-mode verification: see `docs/functional-ui-audit.md` §1.5 for
  the full test matrix (desktop/tablet/mobile, light/dark) and the specific
  computed-style pixels sampled to confirm full opacity.
- `npx tsc -b`, `npx oxlint`, `npx vitest run` (84/84 passing across 19
  files), `npm run build` all pass with no new warnings or errors.
