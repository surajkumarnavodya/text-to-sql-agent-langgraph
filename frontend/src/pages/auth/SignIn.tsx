import { Eye, EyeOff, Loader2 } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
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
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
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
      </CardContent>
    </Card>
  )
}
