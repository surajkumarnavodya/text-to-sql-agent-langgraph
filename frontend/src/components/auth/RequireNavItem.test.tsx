import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as navigationApi from '@/lib/navigationApi'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { navigationFor } from '@/test/navigationFixtures'
import { RequireNavItem } from './RequireNavItem'

vi.mock('@/lib/navigationApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/navigationApi')>()
  return { ...actual, getNavigation: vi.fn(), reportAccessDenied: vi.fn() }
})

const initialAuthState = useLocalAuthStore.getState()

function renderAt(path: string, screen: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path={path}
            element={
              <RequireNavItem screen={screen}>
                <p>Protected content</p>
              </RequireNavItem>
            }
          />
          <Route path="/" element={<p>Home</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  useLocalAuthStore.setState({ user: { id: 'u1', roles: ['user'] } } as never)
})

afterEach(() => {
  vi.clearAllMocks()
  useLocalAuthStore.setState(initialAuthState, true)
})

describe('RequireNavItem -- loading and error', () => {
  it('shows an explicit loading state, never a blank screen', async () => {
    vi.mocked(navigationApi.getNavigation).mockReturnValue(new Promise(() => {}))
    renderAt('/platform-admin', 'platform_admin')
    expect(screen.getByRole('status')).toHaveTextContent('Checking your access')
    expect(screen.queryByText('Protected content')).not.toBeInTheDocument()
  })

  it('shows an error with a retry, and retry asks the server again', async () => {
    vi.mocked(navigationApi.getNavigation)
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(navigationFor(['platform_admin']))
    renderAt('/platform-admin', 'platform_admin')

    expect(await screen.findByRole('alert')).toHaveTextContent("couldn't check your access")
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByText('Protected content')).toBeInTheDocument()
    expect(navigationApi.getNavigation).toHaveBeenCalledTimes(2)
  })
})

describe('RequireNavItem -- allowed and denied', () => {
  it('renders the screen when the server lists it for this caller', async () => {
    vi.mocked(navigationApi.getNavigation).mockResolvedValue(navigationFor(['platform_admin', 'admin']))
    renderAt('/platform-admin', 'platform_admin')
    expect(await screen.findByText('Protected content')).toBeInTheDocument()
    expect(navigationApi.reportAccessDenied).not.toHaveBeenCalled()
  })

  it('shows the forbidden page, not the screen, when the server does not list it', async () => {
    vi.mocked(navigationApi.getNavigation).mockResolvedValue(navigationFor(['user']))
    renderAt('/platform-admin', 'platform_admin')

    expect(
      await screen.findByRole('heading', { name: "You don't have access to this screen" }),
    ).toBeInTheDocument()
    expect(screen.queryByText('Protected content')).not.toBeInTheDocument()
  })

  it('reports the refused visit to the server, once, for the path it was on', async () => {
    vi.mocked(navigationApi.getNavigation).mockResolvedValue(navigationFor(['user']))
    renderAt('/platform-admin', 'platform_admin')
    await screen.findByRole('heading', { name: "You don't have access to this screen" })

    await waitFor(() => expect(navigationApi.reportAccessDenied).toHaveBeenCalledTimes(1))
    expect(navigationApi.reportAccessDenied).toHaveBeenCalledWith('/platform-admin')
  })

  it('moves focus to the forbidden heading so the change is announced', async () => {
    vi.mocked(navigationApi.getNavigation).mockResolvedValue(navigationFor(['user']))
    renderAt('/platform-admin', 'platform_admin')
    const heading = await screen.findByRole('heading', { name: "You don't have access to this screen" })
    await waitFor(() => expect(heading).toHaveFocus())
  })

  it('links to a screen the caller can use, never to one they cannot', async () => {
    vi.mocked(navigationApi.getNavigation).mockResolvedValue(navigationFor(['user']))
    renderAt('/platform-admin', 'platform_admin')
    const link = await screen.findByRole('link', { name: 'Go to a screen you can use' })
    expect(link).toHaveAttribute('href', '/')
  })

  it('a caller with no screens at all gets no dead-end link', async () => {
    vi.mocked(navigationApi.getNavigation).mockResolvedValue(navigationFor([]))
    renderAt('/platform-admin', 'platform_admin')
    await screen.findByRole('heading', { name: "You don't have access to this screen" })
    expect(screen.queryByRole('link', { name: 'Go to a screen you can use' })).not.toBeInTheDocument()
  })
})
