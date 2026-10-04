import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '@/App'
import * as navigationApi from '@/lib/navigationApi'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { navigationFor, serveNavigation } from '@/test/navigationFixtures'

/** Role x URL matrix for the guarded routes (Prompt 32). Each role is signed in
 * with the server's navigation for it; each URL is opened directly, the way a
 * user typing it would reach it. The screen renders only when the server lists
 * it, and the forbidden page appears otherwise. Pages are stubbed so this file
 * tests routing and access, not each screen's own behaviour (those have their
 * own suites). */

vi.mock('@/lib/navigationApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/navigationApi')>()
  return { ...actual, getNavigation: vi.fn(), reportAccessDenied: vi.fn() }
})
vi.mock('@/components/auth/AuthGate', () => ({ AuthGate: ({ children }: { children: unknown }) => children }))
vi.mock('@/components/layout/PwaUpdateBanner', () => ({ PwaUpdateBanner: () => null }))
vi.mock('@/components/layout/Sidebar', () => ({ Sidebar: () => null }))
vi.mock('@/components/layout/MobileNav', () => ({ MobileNav: () => null }))
vi.mock('@/components/layout/UserMenu', () => ({ UserMenu: () => null }))
vi.mock('@/components/layout/SettingsDialog', () => ({ SettingsDialog: () => null }))

vi.mock('@/pages/Chat', () => ({ Chat: () => <p>Page: chat</p> }))
vi.mock('@/pages/KnowledgeSources', () => ({ KnowledgeSources: () => <p>Page: knowledge_sources</p> }))
vi.mock('@/pages/MediaSearch', () => ({ MediaSearch: () => <p>Page: media_search</p> }))
vi.mock('@/pages/DatabaseOnboarding', () => ({ DatabaseOnboarding: () => <p>Page: db_onboarding</p> }))
vi.mock('@/pages/Recommendations', () => ({ Recommendations: () => <p>Page: recommendations</p> }))
vi.mock('@/pages/SemanticReview', () => ({ SemanticReview: () => <p>Page: semantic_review</p> }))
vi.mock('@/pages/PlatformAdmin', () => ({ PlatformAdmin: () => <p>Page: platform_admin</p> }))
vi.mock('@/pages/TenantAdmin', () => ({ TenantAdmin: () => <p>Page: tenant_admin</p> }))
vi.mock('@/pages/AcceptInvitation', () => ({ AcceptInvitation: () => null }))
vi.mock('@/pages/AuthCallback', () => ({ AuthCallback: () => null }))
vi.mock('@/pages/SharedConversation', () => ({ SharedConversation: () => null }))

const initialAuthState = useLocalAuthStore.getState()

/** Each guarded URL and the screen id that owns it. */
const ROUTES: { url: string; screenId: string; label: string }[] = [
  { url: '/', screenId: 'chat', label: 'Page: chat' },
  { url: '/knowledge-sources', screenId: 'knowledge_sources', label: 'Page: knowledge_sources' },
  { url: '/media-search', screenId: 'media_search', label: 'Page: media_search' },
  { url: '/db-onboarding', screenId: 'db_onboarding', label: 'Page: db_onboarding' },
  { url: '/recommended-actions', screenId: 'recommendations', label: 'Page: recommendations' },
  { url: '/semantic-review', screenId: 'semantic_review', label: 'Page: semantic_review' },
  { url: '/tenant-admin', screenId: 'tenant_admin', label: 'Page: tenant_admin' },
  { url: '/platform-admin', screenId: 'platform_admin', label: 'Page: platform_admin' },
]

/** One row per persona. `null` means signed out (no user, no navigation). */
const PERSONAS: { name: string; roles: string[] | null }[] = [
  { name: 'Platform Super Admin', roles: ['platform_admin', 'admin'] },
  { name: 'Tenant Admin', roles: ['admin'] },
  { name: 'SME / Semantic Reviewer', roles: ['analyst'] },
  { name: 'Business User', roles: ['user'] },
  { name: 'Read-only Viewer', roles: ['auditor'] },
  { name: 'Signed out', roles: null },
]

function renderAppAt(url: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[url]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(navigationApi.getNavigation).mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
  useLocalAuthStore.setState(initialAuthState, true)
})

describe.each(PERSONAS)('$name -- guarded URLs', ({ roles }) => {
  beforeEach(() => {
    useLocalAuthStore.setState(
      (roles === null ? { user: null } : { user: { id: 'u1', roles } }) as never,
    )
    serveNavigation(vi.mocked(navigationApi.getNavigation), roles)
  })

  it.each(ROUTES)('$url renders its screen only when the server lists it', async ({ url, screenId, label }) => {
    renderAppAt(url)
    const allowed = roles !== null && navigationFor(roles).items.some((item) => item.id === screenId)

    if (allowed) {
      expect(await screen.findByText(label)).toBeInTheDocument()
      expect(
        screen.queryByRole('heading', { name: "You don't have access to this screen" }),
      ).not.toBeInTheDocument()
    } else {
      expect(
        await screen.findByRole('heading', { name: "You don't have access to this screen" }),
      ).toBeInTheDocument()
      expect(screen.queryByText(label)).not.toBeInTheDocument()
      await waitFor(() => expect(navigationApi.reportAccessDenied).toHaveBeenCalledWith(url))
    }
  })
})

describe('header navigation reflects the same server decision', () => {
  it('the header links are the server-listed screens, in registry order', async () => {
    useLocalAuthStore.setState({ user: { id: 'u1', roles: ['analyst'] } } as never)
    serveNavigation(vi.mocked(navigationApi.getNavigation), ['analyst'])
    renderAppAt('/')

    const primary = await screen.findByRole('navigation', { name: 'Primary navigation' })
    expect(within(primary).queryByRole('link', { name: 'Chat' })).not.toBeInTheDocument()
    await userEvent.click(await within(primary).findByRole('button', { name: 'AI Workspace' }))
    const items = screen.getAllByRole('menuitem').map((item) => item.textContent?.trim())
    expect(items).toEqual(
      expect.arrayContaining(['Knowledge Sources', 'Media Search', 'Recommendations', 'Database Onboarding', 'SME Semantic Review']),
    )
    expect(items).not.toContain('Tenant Admin')
    expect(items).not.toContain('Platform Admin')
  })
})
