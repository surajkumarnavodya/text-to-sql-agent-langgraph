# Production UI Checklist

A point-in-time review (2026-09-19) against the areas this kind of
checklist typically covers, cross-referenced to where each was actually
verified. Not a certification that every item is perfect — a record of
what was checked and how.

| Area | Status | Evidence |
|---|---|---|
| Duplicate controls (Settings/Theme/Logout/Search/New Chat) | ✅ None found | `docs/navigation-and-actions.md`'s ownership table (2026-09-18 pass); re-spot-checked this session |
| Settings modal backdrop/z-index/focus | ✅ Correct, one bug fixed | `docs/functional-ui-audit.md` §1 |
| Search correctness | ✅ Verified working | `docs/functional-ui-audit.md` §2 |
| New Chat correctness, no duplicates | ✅ Verified working | `docs/functional-ui-audit.md` §3 |
| Image attachment — local UX | ✅ Working as designed | `docs/functional-ui-audit.md` §4, `docs/image-attachment-flow.md` |
| Image attachment — backend processing | ❌ Not implemented (documented gap, not a UI defect) | `docs/image-attachment-flow.md` |
| Icon/button size and spacing consistency | Not re-audited this pass | Last checked in `docs/ui-production-audit.md` (2026-09-18) |
| Mobile layout (sidebar drawer, composer usability) | Not re-tested at real touch viewports this pass | Verified via headless-Chromium screenshots in the 2026-09-18 pass (`docs/navigation-and-actions.md`'s "Verification" section); not re-run here since no code affecting layout changed |
| Loading/empty/error states (search, history, sidebar) | ✅ Present, unchanged | `ConversationSearchResults.tsx`, `ConversationList.tsx` — spinner/empty/error `role="alert"` states, re-read this session |
| Focus indicators, keyboard navigation | ✅ Verified for Settings specifically this pass | `docs/functional-ui-audit.md` §1.2/1.3 |
| Contrast / dark mode | Not re-tested this pass | Verified in the 2026-09-18 pass (dark-mode screenshot in `docs/navigation-and-actions.md`) |
| Toast/dialog/drawer reuse (no one-off overlays) | ✅ Confirmed | `Dialog`/`Drawer`/`Toast` in `components/ui/` are the only overlay primitives used app-wide; `SettingsDialog` builds on the shared `Dialog`, not a one-off |
| Secrets/PII exposure in frontend | ✅ None found | No API keys or tokens in React source; bearer token held in memory only (existing `authStore`/`localAuthStore` design, unchanged) |
| SQL rendering safety | ✅ Unaffected | Not touched this pass; `SqlEditor`'s CodeMirror-based read-only rendering unchanged |
| Markdown/XSS sanitization | ✅ Unaffected | Not touched this pass; `components/ui/markdown.tsx` unchanged |

## What this pass actually changed

Three files (`AppShell.tsx`, `SettingsDialog.tsx`, `UserMenu.tsx`) to fix
one accessibility bug (focus restoration after closing Settings). No
visual/layout change, no new dependency, no backend change.

## Known, disclosed gaps carried forward (not introduced by this pass)

- Image attachments are not processed server-side (`docs/image-attachment-flow.md`).
- Chat-history search has no pagination UI and no in-conversation
  highlight-the-matched-message behavior (`docs/functional-ui-audit.md` §2.3).
- Settings has no category navigation or in-modal settings search — not
  needed at the current settings-surface size, per
  `docs/settings-modal.md`.
