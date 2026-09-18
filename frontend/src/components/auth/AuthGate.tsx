import { Loader2, LogIn } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { ProfileCompletionBanner } from '@/components/auth/ProfileCompletionBanner'
import { isOidcConfigured } from '@/lib/auth'
import { Register } from '@/pages/auth/Register'
import { SignIn } from '@/pages/auth/SignIn'
import { useAuthStore } from '@/store/authStore'
import { useChatStore } from '@/store/chatStore'
import { useLocalAuthStore } from '@/store/localAuthStore'

function Spinner() {
  return (
    <div className="flex h-screen w-screen items-center justify-center bg-[var(--background)]">
      <Loader2 className="h-6 w-6 animate-spin text-[var(--muted-foreground)]" />
    </div>
  )
}

/** Gates `children` (in practice, the whole authenticated app -- see
 * `App.tsx`, which wraps `<AppShell/>` in this rather than `<App/>` itself,
 * so `/auth/callback` stays reachable) behind a real sign-in whenever
 * *either* auth mechanism this app supports is configured server-side.
 *
 * **Local auth (`identity/`, this app's own accounts) is checked first**
 * and, when the backend has it enabled, is the *only* gate that applies --
 * it's this app's own, most-specific credential (same ordering
 * `authStore.ts`'s `getBearerToken()` uses). Its own `"unauthenticated"`
 * state renders `SignIn`/`Register` inline right here, toggled by
 * `authView`, rather than a redirect to a separate route -- this is what
 * makes "sign up" reachable from the exact screen a logged-out user hits,
 * per this feature's own requirement. Only when local auth is
 * `"unconfigured"` (the backend's `GET /health` says `local_auth_enabled`
 * is `false`) does this fall through to the pre-existing OIDC gate below,
 * completely unchanged from before local auth existed.
 *
 * When **neither** is configured, this is a pure pass-through -- zero
 * behavior change from before either auth feature existed.
 *
 * **Frontend gating is UX only, never the real security boundary** -- the
 * exact same principle `agent/authz.py`'s own docstring states for this
 * app's backend RBAC ("no frontend/UI-level restriction is treated as a
 * security boundary anywhere in this codebase"). Every API route this app
 * calls independently re-validates the bearer token server-side
 * (`api/auth.py::verify_api_key`) regardless of whether this component
 * ever rendered -- a caller who reaches the API directly with no token (or
 * an expired/forged one) gets the same 401 a browser that somehow bypassed
 * this component would. This component exists purely so a legitimate user
 * gets a sign-in screen instead of a wall of failed requests. */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation()

  const localStatus = useLocalAuthStore((state) => state.status)
  const localInitialize = useLocalAuthStore((state) => state.initialize)
  const localUser = useLocalAuthStore((state) => state.user)
  const authView = useLocalAuthStore((state) => state.authView)
  const setAuthView = useLocalAuthStore((state) => state.setAuthView)

  const oidcStatus = useAuthStore((state) => state.status)
  const oidcInitialize = useAuthStore((state) => state.initialize)
  const signIn = useAuthStore((state) => state.signIn)

  useEffect(() => {
    void localInitialize()
    void oidcInitialize()
    // Runs once on mount -- both initialize() references are stable
    // zustand action references, and re-running them on every render would
    // re-attempt silent sign-in repeatedly for no reason.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Loads this user's server-side chat history exactly once per sign-in,
  // and clears it (in-memory only -- never a delete call, see
  // chatStore.clearHistory's own docstring) on sign-out or whenever the
  // *signed-in account itself* changes -- covers both an ordinary logout
  // and switching to a different local account within the same tab
  // without a full page reload, so one user's history can never leak into
  // another's view of the app.
  const previousUserIdRef = useRef<string | null>(null)
  useEffect(() => {
    const currentUserId = localUser?.id ?? null
    if (currentUserId === previousUserIdRef.current) return
    const isSignOutOrSwitch = previousUserIdRef.current !== null
    previousUserIdRef.current = currentUserId
    if (isSignOutOrSwitch) useChatStore.getState().clearHistory()
    if (currentUserId) void useChatStore.getState().hydrateHistoryFromServer()
  }, [localUser?.id])

  if (localStatus === 'loading') {
    return <Spinner />
  }

  if (localStatus === 'authenticated') {
    return (
      <>
        {localUser?.needs_profile_completion && <ProfileCompletionBanner />}
        {children}
      </>
    )
  }

  if (localStatus === 'unauthenticated') {
    return (
      <div className="flex h-screen w-screen flex-col items-center justify-center gap-4 bg-[var(--background)] px-4">
        {authView === 'signIn' ? (
          <SignIn onSwitchToRegister={() => setAuthView('register')} />
        ) : (
          <Register onSwitchToSignIn={() => setAuthView('signIn')} />
        )}
      </div>
    )
  }

  // localStatus === 'unconfigured' from here on -- pre-existing OIDC gate,
  // unmodified.
  if (!isOidcConfigured || oidcStatus === 'authenticated') {
    return <>{children}</>
  }

  if (oidcStatus === 'loading') {
    return <Spinner />
  }

  return (
    <div className="flex h-screen w-screen flex-col items-center justify-center gap-4 bg-[var(--background)] px-4 text-center text-[var(--foreground)]">
      <p className="text-lg font-semibold">{t('auth.signInRequired')}</p>
      <p className="max-w-sm text-sm text-[var(--muted-foreground)]">{t('auth.signInPrompt')}</p>
      <Button variant="primary" onClick={() => void signIn()} className="gap-2">
        <LogIn className="h-4 w-4" />
        {t('auth.signIn')}
      </Button>
    </div>
  )
}
