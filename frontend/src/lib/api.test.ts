import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// api.ts pulls the bearer token from authStore.ts (which itself defers to
// localAuthStore.ts) -- mocked here so a test can control exactly what
// token (if any) is "attached" per call, independent of either store's own
// real refresh-cookie/zustand wiring.
const getBearerToken = vi.fn<() => string | undefined>()
vi.mock('@/store/authStore', () => ({ getBearerToken: () => getBearerToken() }))

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('request() -- 401 refresh-and-retry', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    getBearerToken.mockReturnValue('stale-token')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
    vi.clearAllMocks()
  })

  it(
    'retries once with a fresh token when the handler recovers a session, ' +
      'closing the real gap where a 15-minute access token silently broke ' +
      'every subsequent call until a full page reload',
    async () => {
      const { request, setUnauthorizedHandler } = await import('./api')
      const handler = vi.fn().mockResolvedValue('fresh-token')
      setUnauthorizedHandler(handler)

      fetchMock
        .mockResolvedValueOnce(jsonResponse(401, { detail: 'Missing or invalid Authorization header.' }))
        .mockResolvedValueOnce(jsonResponse(200, { ok: true }))

      const result = await request<{ ok: boolean }>('/ask', { method: 'POST' })

      expect(result).toEqual({ ok: true })
      expect(handler).toHaveBeenCalledTimes(1)
      expect(fetchMock).toHaveBeenCalledTimes(2)
      const secondCallHeaders = fetchMock.mock.calls[1][1].headers as Headers
      expect(secondCallHeaders.get('Authorization')).toBe('Bearer fresh-token')
    },
  )

  it('propagates the original 401 when there is no session left to recover', async () => {
    const { request, setUnauthorizedHandler, ApiError } = await import('./api')
    setUnauthorizedHandler(vi.fn().mockResolvedValue(null))

    fetchMock.mockResolvedValue(
      jsonResponse(401, { detail: 'Missing or invalid Authorization header.' }),
    )

    await expect(request('/ask', { method: 'POST' })).rejects.toMatchObject({
      status: 401,
    } satisfies Partial<InstanceType<typeof ApiError>>)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('never retries a 401 from /auth/* itself, to avoid recursing into its own refresh', async () => {
    const { request, setUnauthorizedHandler } = await import('./api')
    const handler = vi.fn().mockResolvedValue('fresh-token')
    setUnauthorizedHandler(handler)

    fetchMock.mockResolvedValue(jsonResponse(401, { detail: 'invalid refresh token' }))

    await expect(request('/auth/refresh', { method: 'POST' })).rejects.toMatchObject({
      status: 401,
    })
    expect(handler).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('shares a single in-flight refresh across concurrent 401s', async () => {
    const { request, setUnauthorizedHandler } = await import('./api')
    let resolveRefresh!: (token: string | null) => void
    const handler = vi.fn(
      () =>
        new Promise<string | null>((resolve) => {
          resolveRefresh = resolve
        }),
    )
    setUnauthorizedHandler(handler)

    fetchMock.mockImplementation((path: string) => {
      if (fetchMock.mock.calls.filter((c) => c[0] === path).length <= 1) {
        return Promise.resolve(jsonResponse(401, { detail: 'expired' }))
      }
      return Promise.resolve(jsonResponse(200, { ok: true }))
    })

    const first = request<{ ok: boolean }>('/ask', { method: 'POST' })
    const second = request<{ ok: boolean }>('/conversations', { method: 'GET' })

    // Let both initial 401s land before the refresh resolves.
    await Promise.resolve()
    await Promise.resolve()
    resolveRefresh('fresh-token')

    await expect(Promise.all([first, second])).resolves.toEqual([{ ok: true }, { ok: true }])
    expect(handler).toHaveBeenCalledTimes(1)
  })
})
