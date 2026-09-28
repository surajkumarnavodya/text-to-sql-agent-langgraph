import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as identityApi from '@/lib/identityApi'
import type { TokenResponse } from '@/lib/types'
import { useLocalAuthStore } from './localAuthStore'

/** Focused coverage for `loginWithGoogle` -- the one action this store
 * gained for Google sign-in (2026-09-28). The rest of this store's
 * pre-existing actions (`login`/`register`/`logout`/`initialize`) have no
 * test file of their own yet -- a pre-existing gap, out of scope to
 * backfill here; this file covers only what was added. */
// Deliberately NOT the `{ ...(await importOriginal()), googleSignIn: vi.fn() }`
// pattern `GoogleSignInButton.test.tsx` uses -- combined with this project's
// global `restoreMocks: true` (vitest.config.ts), that pattern caused
// `identityApi.googleSignIn` to be silently reset to the REAL implementation
// before each test despite `vi.isMockFunction()` still reporting `true`,
// making `loginWithGoogle` hit a real (and here, invalid-in-jsdom) network
// call. Declaring only the one export this file needs, with no spread,
// sidesteps whatever internal bookkeeping causes that.
vi.mock('@/lib/identityApi', () => ({
  googleSignIn: vi.fn(),
}))

const initialState = useLocalAuthStore.getState()

function fakeTokenResponse(overrides: Partial<TokenResponse> = {}): TokenResponse {
  return {
    access_token: 'fake-access-token',
    token_type: 'bearer',
    expires_in: 900,
    user: {
      id: 'user-1',
      email: 'googleuser@example.com',
      username: null,
      display_name: 'Google User',
      needs_profile_completion: false,
      status: 'active',
      is_email_verified: true,
      roles: ['user'],
      created_at: '2026-09-28T00:00:00Z',
      last_login_at: null,
    },
    ...overrides,
  }
}

describe('localAuthStore.loginWithGoogle', () => {
  beforeEach(() => {
    useLocalAuthStore.setState(initialState, true)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    useLocalAuthStore.setState(initialState, true)
  })

  it('sends the raw credential to googleSignIn and never transforms it', async () => {
    vi.mocked(identityApi.googleSignIn).mockResolvedValue(fakeTokenResponse())

    await useLocalAuthStore.getState().loginWithGoogle('the-raw-id-token')

    expect(identityApi.googleSignIn).toHaveBeenCalledWith('the-raw-id-token')
  })

  it('sets status to authenticated with the returned user and access token on success', async () => {
    const token = fakeTokenResponse()
    vi.mocked(identityApi.googleSignIn).mockResolvedValue(token)

    await useLocalAuthStore.getState().loginWithGoogle('credential')

    const state = useLocalAuthStore.getState()
    expect(state.status).toBe('authenticated')
    expect(state.user).toEqual(token.user)
    expect(state.accessToken).toBe('fake-access-token')
    expect(state.error).toBeNull()
  })

  it('never persists the access token or credential to localStorage/sessionStorage', async () => {
    vi.mocked(identityApi.googleSignIn).mockResolvedValue(fakeTokenResponse())

    await useLocalAuthStore.getState().loginWithGoogle('the-raw-id-token')

    expect(window.localStorage.getItem('fake-access-token')).toBeNull()
    expect(window.sessionStorage.getItem('fake-access-token')).toBeNull()
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i)
      const value = key ? window.localStorage.getItem(key) : null
      expect(value).not.toContain('the-raw-id-token')
    }
  })

  it('propagates a rejection without changing state, leaving the caller to handle the error', async () => {
    vi.mocked(identityApi.googleSignIn).mockRejectedValue(new Error('Google sign-in verification failed.'))

    await expect(useLocalAuthStore.getState().loginWithGoogle('bad-credential')).rejects.toThrow(
      'Google sign-in verification failed.',
    )
    expect(useLocalAuthStore.getState().status).not.toBe('authenticated')
  })
})
