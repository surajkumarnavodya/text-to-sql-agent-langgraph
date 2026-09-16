import { UserManager, WebStorageStateStore } from 'oidc-client-ts'

/** OIDC configuration and the `UserManager` singleton -- 2026 Phase 3
 * security review.
 *
 * `docs/AUTHENTICATION.md` documented OIDC as a real, production-grade
 * backend capability (`security/oidc.py` validates a JWT against any
 * standard identity provider), but the shipped React dashboard had no way
 * to actually obtain one: the SPA's only credential was
 * `VITE_API_AUTH_TOKEN`, a single shared secret baked into the built JS
 * bundle at compile time -- extractable by anyone who can reach the
 * deployed app, and (per `api/auth.py`'s own design) granting full `admin`
 * RBAC to whoever presents it. This module is what closes that gap: a real
 * Authorization Code + PKCE flow, so a production deployment with distinct
 * human users can actually sign each of them in as themselves, with a real,
 * short-lived, per-user access token -- not a bundle-visible static secret.
 *
 * **Configuration is entirely build-time, via `.env`** (see
 * `.env.example`'s OIDC section): `VITE_OIDC_AUTHORITY` + `VITE_OIDC_CLIENT_ID`
 * are the only two required for this feature to activate at all --
 * `isOidcConfigured` is `false` (this module's every other export becomes
 * an unused no-op) when either is unset, which is the common case for a
 * local/single-operator deployment (unchanged, zero-behavior-difference
 * default, exactly like `Settings.auth_mode == "none"` on the backend).
 * These are **not secrets** -- a public OIDC client (Authorization Code +
 * PKCE, no client_secret) is designed to be embedded in a public SPA; the
 * authority/client_id pair alone grants nothing without the user actually
 * completing an interactive login at the identity provider.
 *
 * **Token storage is in-memory only, never `localStorage`/`sessionStorage`.**
 * `InMemoryWebStorage` below backs both the user (token) store and the
 * transient auth-flow state store with a plain in-process `Map` -- nothing
 * OIDC-related is ever written to a browser storage API an XSS payload (or
 * a malicious browser extension) could read after the fact. The real
 * tradeoff this buys: a hard page refresh clears the in-memory token, so
 * `useAuthStore.initialize()` (see that module) always attempts a silent
 * (`prompt=none`, hidden-iframe) re-authentication against the identity
 * provider's own session cookie before ever falling back to showing a
 * sign-in screen -- exactly the same UX a `sessionStorage`-backed store
 * would have given for the common case (an already-logged-in IdP session),
 * without the token itself ever touching a persisted browser store.
 */

const AUTHORITY = import.meta.env.VITE_OIDC_AUTHORITY as string | undefined
const CLIENT_ID = import.meta.env.VITE_OIDC_CLIENT_ID as string | undefined
const SCOPE = (import.meta.env.VITE_OIDC_SCOPE as string | undefined) || 'openid profile'
const REDIRECT_URI =
  (import.meta.env.VITE_OIDC_REDIRECT_URI as string | undefined) ||
  `${window.location.origin}/auth/callback`
const POST_LOGOUT_REDIRECT_URI =
  (import.meta.env.VITE_OIDC_POST_LOGOUT_REDIRECT_URI as string | undefined) ||
  window.location.origin

export const isOidcConfigured = Boolean(AUTHORITY && CLIENT_ID)

/** A `Storage` implementation backed by a plain in-memory `Map` --
 * deliberately never `window.localStorage`/`sessionStorage`. See this
 * module's own docstring for why. */
class InMemoryWebStorage implements Storage {
  private readonly store = new Map<string, string>()

  get length(): number {
    return this.store.size
  }

  clear(): void {
    this.store.clear()
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null
  }

  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null
  }

  removeItem(key: string): void {
    this.store.delete(key)
  }

  setItem(key: string, value: string): void {
    this.store.set(key, value)
  }
}

let userManagerSingleton: UserManager | null = null

/** Returns the process-lifetime `UserManager` singleton, constructing it on
 * first use. Throws if OIDC isn't configured -- every caller must check
 * `isOidcConfigured` first (mirrors the backend's own
 * `security.oidc.validate_token`'s "programming-error case" guard for the
 * equivalent situation). */
export function getUserManager(): UserManager {
  if (!AUTHORITY || !CLIENT_ID) {
    throw new Error(
      'OIDC is not configured -- set VITE_OIDC_AUTHORITY and VITE_OIDC_CLIENT_ID to enable it.',
    )
  }
  if (!userManagerSingleton) {
    const inMemoryStore = new WebStorageStateStore({ store: new InMemoryWebStorage() })
    userManagerSingleton = new UserManager({
      authority: AUTHORITY,
      client_id: CLIENT_ID,
      redirect_uri: REDIRECT_URI,
      post_logout_redirect_uri: POST_LOGOUT_REDIRECT_URI,
      scope: SCOPE,
      response_type: 'code',
      // Silently renews the access token in a hidden iframe before it
      // expires, using the IdP's own session cookie -- no user interaction,
      // no full-page redirect. `useAuthStore` also listens for this to
      // fail (`addSilentRenewError`) and treats that as a sign-out, since a
      // renewal failure usually means the IdP session itself has ended.
      automaticSilentRenew: true,
      userStore: inMemoryStore,
      stateStore: inMemoryStore,
    })
  }
  return userManagerSingleton
}
