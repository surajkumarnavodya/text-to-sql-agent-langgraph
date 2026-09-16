import { Loader2, LogIn } from 'lucide-react'
import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { isOidcConfigured } from '@/lib/auth'
import { useAuthStore } from '@/store/authStore'

/** Gates `children` (in practice, the whole authenticated app -- see
 * `App.tsx`, which wraps `<AppShell/>` in this rather than `<App/>` itself,
 * so `/auth/callback` stays reachable) behind a real, interactive sign-in
 * when OIDC is configured (`VITE_OIDC_AUTHORITY`/`VITE_OIDC_CLIENT_ID`).
 *
 * When OIDC is **not** configured (`status === "unconfigured"`, the common
 * case for a local/single-operator deployment), this is a pure pass-
 * through -- zero behavior change from before this feature existed. This
 * is what makes "add OIDC support" additive rather than a breaking change
 * for every existing deployment: nothing here runs unless an operator
 * explicitly opts in via `.env`.
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
  const status = useAuthStore((state) => state.status)
  const initialize = useAuthStore((state) => state.initialize)
  const signIn = useAuthStore((state) => state.signIn)

  useEffect(() => {
    void initialize()
    // Runs once on mount -- initialize() itself is a stable store action
    // reference (zustand), and re-running it on every render would
    // re-attempt silent sign-in repeatedly for no reason.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (!isOidcConfigured || status === 'authenticated') {
    return <>{children}</>
  }

  if (status === 'loading' || status === 'unconfigured') {
    // "unconfigured" only reaches here transiently, if at all -- the guard
    // above already returns early for it once `initialize()` has had a
    // chance to run; shown as a spinner rather than nothing to avoid a
    // blank-screen flash on the very first render.
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-[var(--background)]">
        <Loader2 className="h-6 w-6 animate-spin text-[var(--muted-foreground)]" />
      </div>
    )
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
