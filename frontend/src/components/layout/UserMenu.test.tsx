import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { UserMenu } from './UserMenu'

// UserMenu is the one consolidated location for Settings/Theme/Sign out
// (see docs/navigation-and-actions.md) -- these tests exist specifically
// to lock in "exactly one of each," not just "each one works."
const initialAuthState = useLocalAuthStore.getState()

describe('UserMenu', () => {
  afterEach(() => {
    useLocalAuthStore.setState(initialAuthState, true)
  })

  it('shows display name and email, and opens with Settings/Theme/Sign out when authenticated', async () => {
    useLocalAuthStore.setState({
      status: 'authenticated',
      user: {
        id: 'u1',
        email: 'ada@example.com',
        username: null,
        display_name: 'Ada Lovelace',
        needs_profile_completion: false,
        status: 'active',
      } as never,
    })
    render(<UserMenu onOpenSettings={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: /account menu/i }))

    expect(screen.getByText('Ada Lovelace')).toBeInTheDocument()
    expect(screen.getByText('ada@example.com')).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /settings/i })).toBeInTheDocument()
    expect(screen.getByText(/^theme$/i)).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /sign out/i })).toBeInTheDocument()
  })

  it('calls onOpenSettings exactly once when Settings is selected', async () => {
    useLocalAuthStore.setState({ status: 'unauthenticated', user: null })
    const onOpenSettings = vi.fn()
    render(<UserMenu onOpenSettings={onOpenSettings} />)

    await userEvent.click(screen.getByRole('button', { name: /account menu/i }))
    await userEvent.click(screen.getByRole('menuitem', { name: /settings/i }))

    expect(onOpenSettings).toHaveBeenCalledTimes(1)
  })

  it('hides Sign out when no one is authenticated, but still offers Settings and Theme', async () => {
    useLocalAuthStore.setState({ status: 'unauthenticated', user: null })
    render(<UserMenu onOpenSettings={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: /account menu/i }))

    expect(screen.queryByRole('menuitem', { name: /sign out/i })).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /settings/i })).toBeInTheDocument()
  })

  it('calls logout exactly once when Sign out is selected', async () => {
    const logout = vi.fn().mockResolvedValue(undefined)
    useLocalAuthStore.setState({
      status: 'authenticated',
      user: {
        id: 'u1',
        email: 'ada@example.com',
        username: null,
        display_name: 'Ada Lovelace',
        needs_profile_completion: false,
        status: 'active',
      } as never,
      logout,
    })
    render(<UserMenu onOpenSettings={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: /account menu/i }))
    await userEvent.click(screen.getByRole('menuitem', { name: /sign out/i }))

    expect(logout).toHaveBeenCalledTimes(1)
  })

  it('closes with Escape', async () => {
    useLocalAuthStore.setState({ status: 'unauthenticated', user: null })
    render(<UserMenu onOpenSettings={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: /account menu/i }))
    expect(screen.getByRole('menuitem', { name: /settings/i })).toBeInTheDocument()

    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('menuitem', { name: /settings/i })).not.toBeInTheDocument()
  })
})
