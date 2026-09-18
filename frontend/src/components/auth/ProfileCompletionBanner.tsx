import { X } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { updateProfile } from '@/lib/identityApi'
import { refreshCurrentLocalUser } from '@/store/localAuthStore'

/** A one-time, non-blocking prompt for an account created before
 * `display_name` became mandatory at sign-up (`LocalUser
 * .needs_profile_completion`) -- rendered by `AuthGate.tsx` above the app,
 * never gating access to it. Dismissible; reappears on the next sign-in
 * until the user actually sets a display name (there is no "don't ask
 * again" flag -- a stale, missing display name is worth a gentle nudge
 * every session, not just once, since it's easy to dismiss and forget). */
export function ProfileCompletionBanner() {
  const { t } = useTranslation()
  const [dismissed, setDismissed] = useState(false)
  const [displayName, setDisplayName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (dismissed) return null

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await updateProfile(displayName)
      await refreshCurrentLocalUser()
      setDismissed(true)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t('auth.localGenericError'))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex items-center gap-3 border-b border-[var(--border)] bg-[var(--muted)] px-4 py-2 text-sm">
      <span className="shrink-0 font-medium">{t('auth.completeProfileTitle')}</span>
      <form className="flex flex-1 items-center gap-2" onSubmit={(event) => void handleSubmit(event)}>
        <input
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          placeholder={t('auth.localDisplayNameLabel')}
          className="h-8 w-48 rounded-md border border-[var(--border)] bg-[var(--background)] px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        />
        <Button type="submit" size="sm" variant="primary" disabled={submitting || !displayName.trim()}>
          {submitting ? t('auth.localSigningIn') : t('auth.completeProfileSave')}
        </Button>
        {error && <span className="text-xs text-[var(--danger)]">{error}</span>}
      </form>
      <Button
        type="button"
        size="icon"
        variant="ghost"
        aria-label={t('auth.completeProfileDismiss')}
        onClick={() => setDismissed(true)}
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </div>
  )
}
