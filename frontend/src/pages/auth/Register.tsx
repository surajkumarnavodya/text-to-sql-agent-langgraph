import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { GoogleSignInButton } from '@/components/auth/GoogleSignInButton'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useHealth } from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import { isTokenResponse } from '@/lib/identityApi'
import { validatePasswordStrength } from '@/lib/passwordPolicy'
import { useLocalAuthStore } from '@/store/localAuthStore'

/** The local-account sign-up form -- the other half of `SignIn.tsx`,
 * toggled by `AuthGate.tsx`. On success, `useLocalAuthStore.register()`
 * already updates the store to `"authenticated"` when the backend returns
 * a `TokenResponse` (the common case, `REQUIRE_EMAIL_VERIFICATION=false`);
 * when it instead returns a `MessageResponse` (verification required), this
 * shows that message inline rather than silently doing nothing, since
 * there's no token to sign in with yet.
 *
 * `displayName` is mandatory (`required`, matching `RegisterRequest
 * .display_name`'s own now-required shape on the backend -- see
 * `identity/display_name.py`) -- client-side validation here is UX only,
 * the backend re-validates unconditionally and is the real gate. Password
 * strength is checked client-side too (`validatePasswordStrength`, a
 * mirror of `identity/password_policy.py`) purely so a weak password is
 * caught before a round trip, not as the enforcement point. */
export function Register({ onSwitchToSignIn }: { onSwitchToSignIn: () => void }) {
  const { t } = useTranslation()
  const register = useLocalAuthStore((state) => state.register)
  const loginWithGoogle = useLocalAuthStore((state) => state.loginWithGoogle)
  const health = useHealth()
  const [googleSubmitting, setGoogleSubmitting] = useState(false)
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [passwordViolations, setPasswordViolations] = useState<string[]>([])
  const [verifyMessage, setVerifyMessage] = useState<string | null>(null)

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setVerifyMessage(null)

    const trimmedDisplayName = displayName.trim()
    if (!trimmedDisplayName) {
      setError(t('auth.localDisplayNameLabel'))
      return
    }
    if (password !== confirmPassword) {
      setError(t('auth.localPasswordMismatch'))
      return
    }
    const violations = validatePasswordStrength(password, { email, displayName: trimmedDisplayName })
    if (violations.length > 0) {
      setPasswordViolations(violations)
      return
    }
    setPasswordViolations([])

    setSubmitting(true)
    try {
      const result = await register(email, password, trimmedDisplayName)
      if (!isTokenResponse(result)) {
        setVerifyMessage(result.message || t('auth.localRegistrationVerifyEmail'))
      }
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
      setError(err instanceof ApiError ? err.message : t('auth.localGenericError'))
    } finally {
      setGoogleSubmitting(false)
    }
  }

  if (verifyMessage) {
    return (
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-base">{t('auth.localRegisterTitle')}</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-sm text-[var(--foreground)]">{verifyMessage}</p>
          <button
            type="button"
            onClick={onSwitchToSignIn}
            className="text-xs text-[var(--accent)] hover:underline"
          >
            {t('auth.localSwitchToSignIn')}
          </button>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle className="text-base">{t('auth.localRegisterTitle')}</CardTitle>
        <p className="text-xs text-[var(--muted-foreground)]">{t('auth.localRegisterSubtitle')}</p>
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
            {t('auth.localDisplayNameLabel')}
            <input
              type="text"
              required
              minLength={2}
              maxLength={100}
              autoComplete="name"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              className="h-9 rounded-md border border-[var(--border)] bg-[var(--background)] px-3 text-sm text-[var(--foreground)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            />
            <span className="font-normal text-[var(--muted-foreground)]">
              {t('auth.localDisplayNameHint')}
            </span>
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-[var(--foreground)]">
            {t('auth.localPasswordLabel')}
            <div className="relative">
              <input
                type={showPassword ? 'text' : 'password'}
                required
                minLength={12}
                maxLength={64}
                autoComplete="new-password"
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value)
                  setPasswordViolations([])
                }}
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
            <span className="font-normal text-[var(--muted-foreground)]">
              {t('auth.localPasswordRequirements')}
            </span>
          </label>
          <label className="flex flex-col gap-1 text-xs font-medium text-[var(--foreground)]">
            {t('auth.localConfirmPasswordLabel')}
            <input
              type={showPassword ? 'text' : 'password'}
              required
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              placeholder={t('auth.localPasswordPlaceholder')}
              className="h-9 rounded-md border border-[var(--border)] bg-[var(--background)] px-3 text-sm text-[var(--foreground)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            />
          </label>
          {passwordViolations.length > 0 && (
            <ul className="list-inside list-disc text-xs text-[var(--danger)]">
              {passwordViolations.map((violation) => (
                <li key={violation}>{violation}</li>
              ))}
            </ul>
          )}
          {error && <p className="text-xs text-[var(--danger)]">{error}</p>}
          <Button type="submit" variant="primary" disabled={submitting} className="mt-1 gap-2">
            {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
            {submitting ? t('auth.localRegistering') : t('auth.localRegisterSubmit')}
          </Button>
          <button
            type="button"
            onClick={onSwitchToSignIn}
            className="text-xs text-[var(--accent)] hover:underline"
          >
            {t('auth.localSwitchToSignIn')}
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
