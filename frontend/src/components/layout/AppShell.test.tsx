import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { AppShell } from './AppShell'

// Sidebar/MobileNav pull in chatStore/localAuthStore and a fair amount of
// their own UI -- irrelevant to what this file actually verifies (that the
// app shell's background content is marked `inert` while Settings is open),
// so they're stubbed to keep this test focused and fast. UserMenu and
// SettingsDialog are stubbed too, replaced with a minimal harness that lets
// the test trigger the exact same `onOpenSettings`/`open` wiring AppShell
// itself owns, without needing real auth/query-client context.
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

function renderAppShell() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<AppShell />} />
      </Routes>
    </MemoryRouter>,
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

  it('shows both review tabs for an admin', () => {
    useLocalAuthStore.setState({ user: { roles: ['admin'] } } as never)
    renderAppShell()
    expect(screen.getByRole('link', { name: /database onboarding/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /sme semantic review/i })).toBeInTheDocument()
  })

  it('shows both review tabs for an analyst', () => {
    useLocalAuthStore.setState({ user: { roles: ['analyst'] } } as never)
    renderAppShell()
    expect(screen.getByRole('link', { name: /database onboarding/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /sme semantic review/i })).toBeInTheDocument()
  })

  it('hides both review tabs for a plain user', () => {
    useLocalAuthStore.setState({ user: { roles: ['user'] } } as never)
    renderAppShell()
    expect(screen.queryByRole('link', { name: /database onboarding/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /sme semantic review/i })).not.toBeInTheDocument()
  })

  it('hides both review tabs when signed out', () => {
    useLocalAuthStore.setState({ user: null } as never)
    renderAppShell()
    expect(screen.queryByRole('link', { name: /database onboarding/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /sme semantic review/i })).not.toBeInTheDocument()
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

  it('hides the Platform Admin tab for a plain tenant admin', () => {
    useLocalAuthStore.setState({ user: { roles: ['admin'] } } as never)
    renderAppShell()
    expect(screen.queryByRole('link', { name: /platform admin/i })).not.toBeInTheDocument()
  })

  it('shows the Platform Admin tab only for the platform_admin role', () => {
    useLocalAuthStore.setState({ user: { roles: ['platform_admin'] } } as never)
    renderAppShell()
    expect(screen.getByRole('link', { name: /platform admin/i })).toBeInTheDocument()
  })

  it('hides the Platform Admin tab when signed out', () => {
    useLocalAuthStore.setState({ user: null } as never)
    renderAppShell()
    expect(screen.queryByRole('link', { name: /platform admin/i })).not.toBeInTheDocument()
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

  it('shows the Tenant Admin tab for an admin', () => {
    useLocalAuthStore.setState({ user: { roles: ['admin'] } } as never)
    renderAppShell()
    expect(screen.getByRole('link', { name: /tenant admin/i })).toBeInTheDocument()
  })

  it('shows the Tenant Admin tab for an auditor', () => {
    useLocalAuthStore.setState({ user: { roles: ['auditor'] } } as never)
    renderAppShell()
    expect(screen.getByRole('link', { name: /tenant admin/i })).toBeInTheDocument()
  })

  it('shows the Tenant Admin tab for a manager', () => {
    useLocalAuthStore.setState({ user: { roles: ['manager'] } } as never)
    renderAppShell()
    expect(screen.getByRole('link', { name: /tenant admin/i })).toBeInTheDocument()
  })

  it('hides the Tenant Admin tab for a plain user or analyst', () => {
    useLocalAuthStore.setState({ user: { roles: ['analyst'] } } as never)
    renderAppShell()
    expect(screen.queryByRole('link', { name: /tenant admin/i })).not.toBeInTheDocument()
  })

  it('hides the Tenant Admin tab when signed out', () => {
    useLocalAuthStore.setState({ user: null } as never)
    renderAppShell()
    expect(screen.queryByRole('link', { name: /tenant admin/i })).not.toBeInTheDocument()
  })
})
