// Shared login helper for every scenario in this directory -- see
// docs/SCALE_OUT_PROMPT.md's Phase 0 section. Logs in as one of the
// LOAD_TEST_USER_COUNT accounts eval/load/seed_users.py creates
// (loadtest-user-0000..NNNN@<domain> / LOAD_TEST_USER_PASSWORD), caching
// the token per VU for the life of that VU's iterations -- a real user
// logs in once per session, not once per request, and k6 launches one JS
// VM per VU that this module-level cache lives in for exactly that reason.
import http from 'k6/http'
import { check } from 'k6'

const USER_COUNT = parseInt(__ENV.LOAD_TEST_USER_COUNT || '50', 10)
const PASSWORD = __ENV.LOAD_TEST_USER_PASSWORD || 'LoadTest!Passw0rd123'
const EMAIL_DOMAIN = __ENV.LOAD_TEST_USER_EMAIL_DOMAIN || 'loadtest.example.internal'
const BASE_URL = __ENV.BASE_URL || 'http://localhost:8001'

let cachedToken = null

function emailForVu(vuId) {
  const index = vuId % USER_COUNT
  return `loadtest-user-${String(index).padStart(4, '0')}@${EMAIL_DOMAIN}`
}

// Returns an `Authorization: Bearer ...` header value, logging in once per
// VU and reusing the token for every subsequent call this VU makes -- a
// second login() call in the same VU is a cache hit, not a second request.
export function login(vuId) {
  if (cachedToken) return cachedToken

  const res = http.post(
    `${BASE_URL}/auth/login`,
    JSON.stringify({ email: emailForVu(vuId), password: PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'auth_login' } },
  )
  check(res, {
    'login succeeded': (r) => r.status === 200,
  })
  if (res.status !== 200) {
    throw new Error(`login failed for VU ${vuId}: ${res.status} ${res.body}`)
  }
  cachedToken = res.json('access_token')
  return cachedToken
}

export function authHeaders(vuId) {
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${login(vuId)}`,
  }
}

export { BASE_URL }
