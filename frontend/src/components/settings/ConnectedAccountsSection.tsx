import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { GoogleSignInButton } from '@/components/auth/GoogleSignInButton'
import { Button } from '@/components/ui/button'
import { useHealth } from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import { linkGoogleAccount, listLinkedIdentities, unlinkGoogleAccount } from '@/lib/identityApi'
import type { LinkedIdentityListResponse } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'

/** "Connected accounts" -- lets an already-signed-in local user link a
 * Google identity to their existing account (the authenticated,
 * explicit-consent path `api/identity_auth.py::link_google_account`'s own
 * docstring calls for, as the safe alternative to auto-merging by email),
 * or unlink one they'd previously linked. Renders nothing unless a local
 * session is actually active -- there is no meaningful "connected
 * accounts" concept before that. */
export function ConnectedAccountsSection() {
  const { t } = useTranslation()
  const status = useLocalAuthStore((state) => state.status)
  const health = useHealth()
  const [data, setData] = useState<LinkedIdentityListResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (status !== 'authenticated') {
      setData(null)
      return
    }
    listLinkedIdentities()
      .then(setData)
      .catch(() => {
        // Best-effort -- the section just shows nothing linked rather than
        // an error for what's a secondary, non-blocking settings panel.
      })
  }, [status])

  if (status !== 'authenticated') return null

  const googleLink = data?.identities.find((identity) => identity.provider === 'google')
  // Unlinking would leave the account with no way to sign in at all if
  // there's no password and this is the only linked identity -- the
  // backend enforces this too (409), but disabling the button here means
  // the user finds out from the UI, not a failed request.
  const unlinkWouldLockAccount = Boolean(
    googleLink && !data?.has_password && (data?.identities.length ?? 0) <= 1,
  )

  const handleLink = async (credential: string) => {
    setError(null)
    setBusy(true)
    try {
      setData(await linkGoogleAccount(credential))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t('auth.localGenericError'))
    } finally {
      setBusy(false)
    }
  }

  const handleUnlink = async () => {
    setError(null)
    setBusy(true)
    try {
      setData(await unlinkGoogleAccount())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t('auth.localGenericError'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {error && <p className="text-xs text-[var(--danger)]">{error}</p>}
      {googleLink ? (
        <div className="flex items-center justify-between gap-2 text-sm">
          <span>{t('auth.googleLinked')}</span>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy || unlinkWouldLockAccount}
            title={unlinkWouldLockAccount ? t('auth.unlinkBlockedNoPassword') : undefined}
            onClick={() => void handleUnlink()}
          >
            {t('auth.unlink')}
          </Button>
        </div>
      ) : health.data?.google_signin_enabled && health.data.google_client_id ? (
        <GoogleSignInButton
          clientId={health.data.google_client_id}
          onCredential={handleLink}
          disabled={busy}
        />
      ) : (
        <p className="text-xs text-[var(--muted-foreground)]">{t('auth.googleUnavailable')}</p>
      )}
    </div>
  )
}
