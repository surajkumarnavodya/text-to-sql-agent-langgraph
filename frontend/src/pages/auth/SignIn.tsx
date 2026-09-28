import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { GoogleSignInButton } from '@/components/auth/GoogleSignInButton'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useHealth } from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import { useLocalAuthStore } from '@/store/localAuthStore'

/** The local-account sign-in form -- rendered by `AuthGate.tsx` (never
 * routed to directly) whenever local auth is configured and no session is
 * currently active. `onSwitchToRegister` toggles `AuthGate`'s own local
 * state to show `Register.tsx` instead, so sign-up is reachable from the
 * exact same screen the user hits the login wall on, per the "give the
 * option to sign up in UI itself" requirement. */
export function SignIn({ onSwitchToRegister }: { onSwitchToRegister: () => void }) {
  const { t } = useTranslation()
  const login = useLocalAuthStore((state) => state.login)
  const loginWithGoogle = useLocalAuthStore((state) => state.loginWithGoogle)
  const health = useHealth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [googleSubmitting, setGoogleSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(email, password)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t('auth.localGenericError'))
    } finally {
      setSubmitting(false)
    }
  }

  const handleGoogleCredential = async (credential: string) => {
    setError(null)
    setGoogleSubmitting(true)
    try {
      await loginWithGoogle(credential)
    } catch (err) {
      // A 409 here means an existing local account already owns this
      // email (see api/identity_auth.py::google_signin's own docstring) --
      // the server's own message already tells the user what to do next
      // (sign in with a password, then link Google from settings), so it's
      // shown as-is rather than papered over with a generic string.
      setError(err instanceof ApiError ? err.message : t('auth.localGenericError'))
    } finally {
      setGoogleSubmitting(false)
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle className="text-base">{t('auth.localSignInTitle')}</CardTitle>
        <p className="text-xs text-[var(--muted-foreground)]">{t('auth.localSignInSubtitle')}</p>
      </CardHeader>
      <CardContent>
        <form className="flex flex-col gap-3" onSubmit={(event) => void handleSubmit(event)}>
          <label className="flex flex-col gap-1 text-xs font-medium text-[var(--foreground)]">
            {t('auth.localEmailLabel')}
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder={t('auth.localEmailPlaceholder')}
              className="h-9 rounded-md border border-[var(--border)] bg-[var(--background)] px-3 text-sm text-[var(--foreground)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            />
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-[var(--foreground)]">
            {t('auth.localPasswordLabel')}
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                required
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder={t('auth.localPasswordPlaceholder')}
                className="h-9 w-full rounded-md border border-[var(--border)] bg-[var(--background)] px-3 pr-9 text-sm text-[var(--foreground)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
              />
              <button
                type="button"
                onClick={() => setShowPassword((current) => !current)}
                aria-label={showPassword ? t('auth.hidePassword') : t('auth.showPassword')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
              >
                {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </label>
          {error && <p className="text-xs text-[var(--danger)]">{error}</p>}
          <Button type="submit" variant="primary" disabled={submitting} className="mt-1 gap-2">
            {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
            {submitting ? t('auth.localSigningIn') : t('auth.localSignInSubmit')}
          </Button>
          <button
            type="button"
            onClick={onSwitchToRegister}
            className="text-xs text-[var(--accent)] hover:underline"
          >
            {t('auth.localSwitchToRegister')}
          </button>
        </form>

        {health.data?.google_signin_enabled && health.data.google_client_id && (
          <div className="mt-4 flex flex-col items-center gap-3">
            <div className="flex w-full items-center gap-2 text-xs text-[var(--muted-foreground)]">
              <span className="h-px flex-1 bg-[var(--border)]" />
              {t('auth.orDivider')}
              <span className="h-px flex-1 bg-[var(--border)]" />
            </div>
            {googleSubmitting ? (
              <div className="flex h-10 items-center gap-2 text-xs text-[var(--muted-foreground)]">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t('auth.googleSigningIn')}
              </div>
            ) : (
              <GoogleSignInButton
                clientId={health.data.google_client_id}
                onCredential={handleGoogleCredential}
              />
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
