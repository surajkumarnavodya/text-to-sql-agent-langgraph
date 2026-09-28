import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as shareApi from '@/lib/shareApi'
import type { Share } from '@/lib/types'
import { ShareModal } from './ShareModal'

vi.mock('@/lib/shareApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/shareApi')>()
  return {
    ...actual,
    getShare: vi.fn(),
    createShare: vi.fn(),
    updateShare: vi.fn(),
    revokeShare: vi.fn(),
    regenerateShareLink: vi.fn(),
    inviteShareMember: vi.fn(),
    removeShareMember: vi.fn(),
  }
})

const { FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    status: number
    constructor(status: number) {
      super('not found')
      this.status = status
    }
  }
  return { FakeApiError }
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, ApiError: FakeApiError }
})

function makeShare(overrides: Partial<Share> = {}): Share {
  return {
    id: 'share-1',
    conversation_id: 'conv-1',
    access_mode: 'invite_only',
    default_permission: 'viewer',
    status: 'active',
    snapshot_message_sequence: 2,
    snapshot_captured_at: '2026-01-01T00:00:00Z',
    expires_at: '2026-02-01T00:00:00Z',
    revoked_at: null,
    version: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    members: [],
    link_available: false,
    member_view_path: '/shared/share-1',
    ...overrides,
  }
}

describe('ShareModal', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows an "enable sharing" prompt when the conversation has no share yet', async () => {
    vi.mocked(shareApi.getShare).mockRejectedValue(new FakeApiError(404))
    render(<ShareModal conversationId="conv-1" conversationTitle="My chat" onClose={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/isn't shared yet/i)).toBeInTheDocument())
  })

  it('creating a share for the first time shows the snapshot notice and settings', async () => {
    vi.mocked(shareApi.getShare).mockRejectedValue(new FakeApiError(404))
    vi.mocked(shareApi.createShare).mockResolvedValue({ share: makeShare(), link: null })
    const user = userEvent.setup()
    render(<ShareModal conversationId="conv-1" conversationTitle="My chat" onClose={vi.fn()} />)

    await waitFor(() => screen.getByRole('button', { name: /enable sharing/i }))
    await user.click(screen.getByRole('button', { name: /enable sharing/i }))

    await waitFor(() => expect(screen.getByText(/only invited people/i)).toBeInTheDocument())
  })

  it('inviting a member calls the API with the entered email and shows the invite link once', async () => {
    vi.mocked(shareApi.getShare).mockResolvedValue(makeShare())
    vi.mocked(shareApi.inviteShareMember).mockResolvedValue({
      member: {
        id: 'member-1',
        user_id: null,
        invited_email: 'friend@example.com',
        display_name: null,
        role: 'viewer',
        status: 'pending',
        expires_at: null,
        accepted_at: null,
        revoked_at: null,
        created_at: '2026-01-01T00:00:00Z',
      },
      invitation_path: '/accept-invitation/raw-token-abc',
    })
    const user = userEvent.setup()
    render(<ShareModal conversationId="conv-1" conversationTitle="My chat" onClose={vi.fn()} />)

    await waitFor(() => screen.getByPlaceholderText(/email address/i))
    await user.type(screen.getByPlaceholderText(/email address/i), 'friend@example.com')
    await user.click(screen.getByRole('button', { name: /^invite$/i }))

    await waitFor(() =>
      expect(shareApi.inviteShareMember).toHaveBeenCalledWith('conv-1', { email: 'friend@example.com' }),
    )
    await waitFor(() => expect(screen.getByText(/raw-token-abc/)).toBeInTheDocument())
  })

  it('switching to "anyone with the link" requires confirmation before widening access', async () => {
    vi.mocked(shareApi.getShare).mockResolvedValue(makeShare())
    const user = userEvent.setup()
    render(<ShareModal conversationId="conv-1" conversationTitle="My chat" onClose={vi.fn()} />)

    await waitFor(() => screen.getByLabelText(/anyone with the link/i))
    await user.click(screen.getByLabelText(/anyone with the link/i))

    expect(screen.getByText(/anyone who has this link/i)).toBeInTheDocument()
    // Not applied yet -- the update call requires the explicit confirm click.
    expect(shareApi.updateShare).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /^confirm$/i }))
    await waitFor(() => expect(shareApi.updateShare).toHaveBeenCalledWith('conv-1', {
      version: 1,
      access_mode: 'anyone_with_link',
    }))
  })

  it('revoking requires a confirmation step before calling the API', async () => {
    vi.mocked(shareApi.getShare).mockResolvedValue(makeShare())
    vi.mocked(shareApi.revokeShare).mockResolvedValue(makeShare({ status: 'disabled' }))
    const user = userEvent.setup()
    render(<ShareModal conversationId="conv-1" conversationTitle="My chat" onClose={vi.fn()} />)

    await waitFor(() => screen.getByRole('button', { name: /revoke share/i }))
    await user.click(screen.getByRole('button', { name: /revoke share/i }))
    expect(shareApi.revokeShare).not.toHaveBeenCalled()

    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument()
    const confirmButtons = screen.getAllByRole('button', { name: /revoke share/i })
    await user.click(confirmButtons[confirmButtons.length - 1])

    await waitFor(() => expect(shareApi.revokeShare).toHaveBeenCalledWith('conv-1', 1))
  })

  it('never renders the raw link value anywhere except right after create/regenerate', async () => {
    vi.mocked(shareApi.getShare).mockResolvedValue(makeShare({ access_mode: 'invite_only' }))
    render(<ShareModal conversationId="conv-1" conversationTitle="My chat" onClose={vi.fn()} />)

    await waitFor(() => screen.getByText(/only invited people/i))
    expect(screen.queryByText(/\/shared\//)).not.toBeInTheDocument()
  })
})
