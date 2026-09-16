import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/store/authStore'

/** The OIDC `redirect_uri` target (`VITE_OIDC_REDIRECT_URI`, default
 * `${origin}/auth/callback`) -- the identity provider sends the user back
 * here after a successful interactive login, carrying the authorization
 * code in the URL. `completeSignIn()` exchanges it for tokens (still
 * server-side, via the IdP, using PKCE -- no client secret in this public
 * SPA), then this redirects to the app's home route. Rendered *outside*
 * `AuthGate` (see `App.tsx`) -- it must be reachable before the user is
 * considered signed in, or the callback could never complete. */
export function AuthCallback() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const completeSignIn = useAuthStore((state) => state.completeSignIn)
  const [error, setError] = useState<string | null>(null)
  // StrictMode double-invokes effects in development; the authorization
  // code in the URL is single-use, so a second exchange attempt would
  // fail even on the genuinely successful path -- this guards against
  // running the exchange twice.
  const attempted = useRef(false)

  useEffect(() => {
    if (attempted.current) return
    attempted.current = true
    completeSignIn()
      .then(() => navigate('/', { replace: true }))
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : String(err))
      })
  }, [completeSignIn, navigate])

  return (
    <div className="flex h-screen w-screen flex-col items-center justify-center gap-3 bg-[var(--background)] text-[var(--foreground)]">
      {error ? (
        <>
          <p className="text-sm font-medium text-[var(--danger)]">{t('auth.signInFailed')}</p>
          <p className="max-w-md text-center text-xs text-[var(--muted-foreground)]">{error}</p>
        </>
      ) : (
        <p className="text-sm text-[var(--muted-foreground)]">{t('auth.signingIn')}</p>
      )}
    </div>
  )
}
