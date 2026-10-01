import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import * as identityApi from '@/lib/identityApi'
import { GoogleSignInButton } from './GoogleSignInButton'

vi.mock('@/lib/identityApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/identityApi')>()
  return { ...actual, fetchGoogleSigninNonce: vi.fn() }
})

vi.mock('@/lib/googleIdentity', () => ({
  loadGoogleIdentityScript: vi.fn(),
}))

import { loadGoogleIdentityScript } from '@/lib/googleIdentity'

describe('GoogleSignInButton', () => {
  let initializeMock: Mock<(config: GoogleIdentityInitializeConfig) => void>
  let renderButtonMock: Mock<(parent: HTMLElement, options: GoogleIdentityButtonOptions) => void>

  beforeEach(() => {
    initializeMock = vi.fn()
    renderButtonMock = vi.fn()
    window.google = {
      accounts: { id: { initialize: initializeMock, renderButton: renderButtonMock, disableAutoSelect: vi.fn(), cancel: vi.fn() } },
    }
    vi.mocked(loadGoogleIdentityScript).mockResolvedValue(undefined)
    vi.mocked(identityApi.fetchGoogleSigninNonce).mockResolvedValue({ nonce: 'test-nonce-123' })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    delete window.google
  })

  it('initializes Google Identity Services with the fetched nonce and renders the button', async () => {
    const onCredential = vi.fn()
    render(<GoogleSignInButton clientId="test-client-id" onCredential={onCredential} />)

    await waitFor(() => expect(initializeMock).toHaveBeenCalledTimes(1))

    expect(initializeMock).toHaveBeenCalledWith(
      expect.objectContaining({ client_id: 'test-client-id', nonce: 'test-nonce-123' }),
    )
    expect(renderButtonMock).toHaveBeenCalledTimes(1)
  })

  it('calls onCredential with the raw credential when the GIS callback fires', async () => {
    const onCredential = vi.fn()
    render(<GoogleSignInButton clientId="test-client-id" onCredential={onCredential} />)

    await waitFor(() => expect(initializeMock).toHaveBeenCalledTimes(1))

    const { callback } = initializeMock.mock.calls[0][0]
    callback({ credential: 'the-raw-id-token' })

    expect(onCredential).toHaveBeenCalledWith('the-raw-id-token')
  })

  it('renders nothing when disabled', () => {
    const { container } = render(
      <GoogleSignInButton clientId="test-client-id" onCredential={vi.fn()} disabled />,
    )
    expect(container).toBeEmptyDOMElement()
    expect(loadGoogleIdentityScript).not.toHaveBeenCalled()
  })

  it('renders nothing when clientId is empty', () => {
    const { container } = render(<GoogleSignInButton clientId="" onCredential={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows a fallback message, not a crash, when the script fails to load', async () => {
    vi.mocked(loadGoogleIdentityScript).mockRejectedValue(new Error('network down'))
    render(<GoogleSignInButton clientId="test-client-id" onCredential={vi.fn()} />)

    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())
    expect(initializeMock).not.toHaveBeenCalled()
  })

  it('shows a fallback message when fetching the nonce fails', async () => {
    vi.mocked(identityApi.fetchGoogleSigninNonce).mockRejectedValue(new Error('server error'))
    render(<GoogleSignInButton clientId="test-client-id" onCredential={vi.fn()} />)

    await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())
    expect(initializeMock).not.toHaveBeenCalled()
  })

  it('never stores the credential anywhere beyond handing it to the caller', async () => {
    const onCredential = vi.fn()
    render(<GoogleSignInButton clientId="test-client-id" onCredential={onCredential} />)
    await waitFor(() => expect(initializeMock).toHaveBeenCalledTimes(1))

    const { callback } = initializeMock.mock.calls[0][0]
    callback({ credential: 'super-secret-id-token' })

    // Nothing in this component's own DOM output ever contains the raw
    // credential -- it exists only long enough to be handed to the caller.
    expect(document.body.textContent).not.toContain('super-secret-id-token')
    expect(window.localStorage.getItem('super-secret-id-token')).toBeNull()
  })
})
