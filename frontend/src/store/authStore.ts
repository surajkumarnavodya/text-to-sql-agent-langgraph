import type { User } from 'oidc-client-ts'
import { create } from 'zustand'
import { getUserManager, isOidcConfigured } from '@/lib/auth'
import { getLocalBearerToken } from '@/store/localAuthStore'

/** `AuthGate.tsx` renders based on this:
 * - `"unconfigured"`: OIDC isn't set up at all -- render the app exactly
 *   as it always was (the static-token/no-auth flow this app had before
 *   this feature existed), no gate, no login screen.
 * - `"loading"`: attempting silent (no-interaction) re-authentication
 *   against the IdP's own session on app load -- shows a spinner, not a
 *   login screen, since this usually resolves in well under a second for
 *   an already-logged-in user.
 * - `"authenticated"` / `"unauthenticated"`: self-explanatory.
 */
export type AuthStatus = 'unconfigured' | 'loading' | 'authenticated' | 'unauthenticated'

interface AuthState {
  status: AuthStatus
  user: User | null
  error: string | null
  /** Runs once, on app load -- attempts a silent re-authentication (no
   * user interaction, works even though the token store is in-memory-only
   * and a hard refresh just cleared it, as long as the IdP's own session
   * cookie is still valid) before ever falling back to `"unauthenticated"`. */
  initialize: () => Promise<void>
  /** Kicks off the real, interactive Authorization Code + PKCE flow --
   * redirects the whole page to the identity provider. */
  signIn: () => Promise<void>
  signOut: () => Promise<void>
  /** Called only by `AuthCallback.tsx`, the `redirect_uri` target the IdP
   * sends the user back to after a successful interactive login. */
  completeSignIn: () => Promise<User>
}

// Only a fallback for service/CI-style callers, and only ever consulted
// when OIDC itself isn't configured -- mirrors api/auth.py's own
// auth_mode dispatch order (OIDC tried first, a static shared secret is
// the fallback), applied to which credential this SPA itself presents.
const STATIC_API_TOKEN = import.meta.env.VITE_API_AUTH_TOKEN as string | undefined

export const useAuthStore = create<AuthState>((set) => ({
  status: isOidcConfigured ? 'loading' : 'unconfigured',
  user: null,
  error: null,

  initialize: async () => {
    if (!isOidcConfigured) return
    const manager = getUserManager()

    manager.events.addUserLoaded((user) => set({ user, status: 'authenticated', error: null }))
    manager.events.addUserUnloaded(() => set({ user: null, status: 'unauthenticated' }))
    // A silent-renew failure almost always means the IdP's own session
    // ended (expired, or the user signed out elsewhere) -- treated as a
    // sign-out rather than a transient error, so the sign-in screen
    // reappears instead of the app silently continuing on a token that's
    // about to stop working.
    manager.events.addSilentRenewError(() => set({ user: null, status: 'unauthenticated' }))

    try {
      const user = await manager.signinSilent()
      set({ user, status: user ? 'authenticated' : 'unauthenticated', error: null })
    } catch {
      // No existing IdP session (or it's expired) -- not an error state,
      // just "not signed in yet." AuthGate shows the sign-in screen.
      set({ user: null, status: 'unauthenticated' })
    }
  },

  signIn: async () => {
    await getUserManager().signinRedirect()
  },

  signOut: async () => {
    try {
      await getUserManager().signoutRedirect()
    } catch {
      // The configured provider may not support/expose an end-session
      // endpoint -- fails safe by clearing the local session even if the
      // IdP-side signout redirect itself couldn't be started, rather than
      // leaving the user stuck "signed in" in this tab.
      await getUserManager().removeUser()
      set({ user: null, status: 'unauthenticated' })
    }
  },

  completeSignIn: async () => {
    const user = await getUserManager().signinRedirectCallback()
    set({ user, status: 'authenticated', error: null })
    return user
  },
}))

/** The bearer token every `frontend/src/lib/api.ts` call attaches, if any.
 * Priority order: this app's own local account (`localAuthStore.ts`, if
 * signed in) -> OIDC's per-user access token (if configured and signed
 * in) -> `STATIC_API_TOKEN` (service/CI callers, or neither is
 * configured) -- never more than one at a time, and never a token pulled
 * from `localStorage`/`sessionStorage` (see `lib/auth.ts`'s own docstring
 * for why). Local auth is checked first since it's this app's own,
 * most-specific credential -- mirrors `api/auth.py::verify_api_key`'s own
 * local-JWT-first dispatch order. */
export function getBearerToken(): string | undefined {
  const localToken = getLocalBearerToken()
  if (localToken) return localToken
  if (isOidcConfigured) {
    return useAuthStore.getState().user?.access_token ?? STATIC_API_TOKEN
  }
  return STATIC_API_TOKEN
}
