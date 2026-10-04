import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useLocalAuthStore } from '@/store/localAuthStore'
import * as navigationApi from '@/lib/navigationApi'
import { serveNavigation } from '@/test/navigationFixtures'
import { AppShell } from './AppShell'

// Sidebar/MobileNav pull in chatStore/localAuthStore and a fair amount of
// their own UI -- irrelevant to what this file actually verifies (that the
// app shell's background content is marked `inert` while Settings is open),
// so they're stubbed to keep this test focused and fast. UserMenu and
// SettingsDialog are stubbed too, replaced with a minimal harness that lets
// the test trigger the exact same `onOpenSettings`/`open` wiring AppShell
// itself owns, without needing real auth/query-client context.
vi.mock('@/lib/navigationApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/navigationApi')>()
  return { ...actual, getNavigation: vi.fn(), reportAccessDenied: vi.fn() }
})
vi.mock('./Sidebar', () => ({ Sidebar: () => <div>Sidebar stub</div> }))
vi.mock('./MobileNav', () => ({ MobileNav: () => <div>MobileNav stub</div> }))
vi.mock('./UserMenu', () => ({
  UserMenu: ({ onOpenSettings }: { onOpenSettings: () => void }) => (
    <button onClick={onOpenSettings}>Open settings</button>
  ),
}))
vi.mock('./SettingsDialog', () => ({
  SettingsDialog: ({ open }: { open: boolean }) =>
    open ? <div role="dialog">Settings stub</div> : null,
}))

/** Signs a caller in with `roles` and serves the server's navigation for them. */
function signInAs(roles: string[]) {
  useLocalAuthStore.setState({ user: { roles } } as never)
  serveNavigation(vi.mocked(navigationApi.getNavigation), roles)
}

/** Signed out: no user, and an empty navigation response. */
function signOut() {
  useLocalAuthStore.setState({ user: null } as never)
  serveNavigation(vi.mocked(navigationApi.getNavigation), null)
}

beforeEach(() => serveNavigation(vi.mocked(navigationApi.getNavigation), null))

/** Non-Chat screens live in the header's Menu dropdown. Opens it (once) and
 * returns the matching menu item, or null if no menu is rendered at all. */
async function findScreenItem(name: RegExp) {
  // `hidden: true`: while the menu is open, Radix marks the rest of the page
  // aria-hidden, so the trigger must be looked up past that to toggle it.
  const trigger = screen.queryByRole('button', { name: 'AI Workspace', hidden: true })
  if (!trigger) return null
  if (trigger.getAttribute('aria-expanded') !== 'true') await userEvent.click(trigger)
  return screen.queryByRole('menuitem', { name })
}

/** Renders and waits until the server navigation response has been applied.
 * Without this, a `queryByRole(...).not` assertion passes before any tab data
 * has arrived, which proves nothing. */
async function renderAndSettle() {
  renderAppShell()
  await waitFor(() => expect(vi.mocked(navigationApi.getNavigation)).toHaveBeenCalled())
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0))
  })
}

function renderAppShell() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<AppShell />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AppShell background inert-while-modal-open', () => {
  it('does not mark the background content inert before Settings opens', () => {
    renderAppShell()
    const background = screen.getByText('Sidebar stub').closest('div.flex.h-full')
    expect(background).not.toBeNull()
    expect(background).not.toHaveAttribute('inert')
  })

  // Regression coverage for docs/settings-modal-visual-bug.md: the opaque
  // backdrop (ui/dialog.tsx) handles *visual* occlusion, but the app shell
  // behind it must also become unreachable to keyboard/assistive-tech
  // navigation while Settings is open -- `inert` is what removes it from
  // the accessibility tree and tab order on top of (not instead of) the
  // opaque backdrop.
  it('marks the background content inert once Settings is open', async () => {
    renderAppShell()
    await userEvent.click(screen.getByRole('button', { name: 'Open settings' }))

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    const background = screen.getByText('Sidebar stub').closest('div.flex.h-full')
    expect(background).toHaveAttribute('inert')
  })
})

// Prompt 26/27: Database Onboarding and SME Semantic Review are the two nav
// tabs gated on role (UX only -- see each page's own docstring for why the
// real boundary stays server-side). A regression here (an earlier version
// of the gating selector returned a freshly-allocated array on every call,
// which breaks Zustand's snapshot-equality check and crashes the whole
// shell with "Maximum update depth exceeded" -- caught by this file's
// pre-existing tests above, not a hypothetical) would silently hide the
// feature for an admin or show it to everyone.
describe('AppShell role-gated review-tool nav gating', () => {
  const initialAuthState = useLocalAuthStore.getState()

  afterEach(() => {
    useLocalAuthStore.setState(initialAuthState, true)
  })

  it('shows both review tabs for an admin', async () => {
    signInAs(['admin'])
    await renderAndSettle()
    expect(await findScreenItem(/database onboarding/i)).toBeInTheDocument()
    expect(await findScreenItem(/sme semantic review/i)).toBeInTheDocument()
  })

  it('shows both review tabs for an analyst', async () => {
    signInAs(['analyst'])
    await renderAndSettle()
    expect(await findScreenItem(/database onboarding/i)).toBeInTheDocument()
    expect(await findScreenItem(/sme semantic review/i)).toBeInTheDocument()
  })

  it('hides both review tabs for a plain user', async () => {
    signInAs(['user'])
    await renderAndSettle()
    expect(await findScreenItem(/database onboarding/i)).toBeNull()
    expect(await findScreenItem(/sme semantic review/i)).toBeNull()
  })

  it('hides both review tabs when signed out', async () => {
    signOut()
    await renderAndSettle()
    expect(await findScreenItem(/database onboarding/i)).toBeNull()
    expect(await findScreenItem(/sme semantic review/i)).toBeNull()
  })
})

// Prompt 28: Platform Admin is gated on a *different* dimension than the
// two review tabs above -- the `platform_admin` role, never satisfied by
// `admin` alone (see `identity.rbac.Permission.PLATFORM_ADMIN`'s own
// docstring for the full "platform-admin versus tenant-admin" rationale).
// A regression here that folded this into `canSeeReviewTabs` would show a
// tenant's own admin a tab that 403s for them every time.
describe('AppShell Platform Admin nav gating', () => {
  const initialAuthState = useLocalAuthStore.getState()

  afterEach(() => {
    useLocalAuthStore.setState(initialAuthState, true)
  })

  it('hides the Platform Admin tab for a plain tenant admin', async () => {
    signInAs(['admin'])
    await renderAndSettle()
    expect(await findScreenItem(/platform admin/i)).toBeNull()
  })

  it('shows the Platform Admin tab only for the platform_admin role', async () => {
    signInAs(['platform_admin'])
    await renderAndSettle()
    expect(await findScreenItem(/platform admin/i)).toBeInTheDocument()
  })

  it('hides the Platform Admin tab when signed out', async () => {
    signOut()
    await renderAndSettle()
    expect(await findScreenItem(/platform admin/i)).toBeNull()
  })
})

// Prompt 29: Tenant Admin introduces no new role at all -- it's visible to
// admin/auditor/manager (identity.rbac.Permission.ADMIN_DASHBOARD_READ's
// own real grant set), a different set from canSeeReviewTabs's
// admin/analyst (an analyst has no business-administration capability).
describe('AppShell Tenant Admin nav gating', () => {
  const initialAuthState = useLocalAuthStore.getState()

  afterEach(() => {
    useLocalAuthStore.setState(initialAuthState, true)
  })

  it('shows the Tenant Admin tab for an admin', async () => {
    signInAs(['admin'])
    await renderAndSettle()
    expect(await findScreenItem(/tenant admin/i)).toBeInTheDocument()
  })

  it('shows the Tenant Admin tab for an auditor', async () => {
    signInAs(['auditor'])
    await renderAndSettle()
    expect(await findScreenItem(/tenant admin/i)).toBeInTheDocument()
  })

  it('shows the Tenant Admin tab for a manager', async () => {
    signInAs(['manager'])
    await renderAndSettle()
    expect(await findScreenItem(/tenant admin/i)).toBeInTheDocument()
  })

  it('hides the Tenant Admin tab for a plain user or analyst', async () => {
    signInAs(['analyst'])
    await renderAndSettle()
    expect(await findScreenItem(/tenant admin/i)).toBeNull()
  })

  it('hides the Tenant Admin tab when signed out', async () => {
    signOut()
    await renderAndSettle()
    expect(await findScreenItem(/tenant admin/i)).toBeNull()
  })
})
