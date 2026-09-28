import { CheckCircle2, Loader2, XCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '@/lib/api'
import { acceptShareInvitation } from '@/lib/shareApi'

type AcceptState = 'accepting' | 'accepted' | 'error'

/** Redeems a share invitation for the *currently signed-in* account
 * (`POST /share-invitations/{token}/accept`) -- reached inside `AuthGate`
 * (see `App.tsx`), so an unauthenticated visitor sees the ordinary sign-in
 * screen first and lands back here once signed in, matching this app's
 * existing "sign in, then continue" pattern rather than a separate
 * bespoke redirect flow. */
export function AcceptInvitation() {
  const { token } = useParams<{ token: string }>()
  const { t } = useTranslation()
  const [state, setState] = useState<AcceptState>('accepting')
  const [memberViewPath, setMemberViewPath] = useState<string | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  useEffect(() => {
    if (!token) return
    let cancelled = false
    async function accept() {
      try {
        const result = await acceptShareInvitation(token!)
        if (!cancelled) {
          setMemberViewPath(result.member_view_path)
          setState('accepted')
        }
      } catch (err) {
        if (cancelled) return
        setErrorMessage(err instanceof ApiError ? err.message : t('acceptInvitation.genericError'))
        setState('error')
      }
    }
    void accept()
    return () => {
      cancelled = true
    }
  }, [token, t])

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-[var(--background)] px-4 text-center text-[var(--foreground)]">
      {state === 'accepting' && (
        <>
          <Loader2 className="h-6 w-6 animate-spin text-[var(--muted-foreground)]" />
          <p className="text-sm text-[var(--muted-foreground)]">{t('acceptInvitation.working')}</p>
        </>
      )}
      {state === 'accepted' && memberViewPath && (
        <>
          <CheckCircle2 className="h-6 w-6 text-[var(--success)]" />
          <p className="text-sm">{t('acceptInvitation.success')}</p>
          <Link to={memberViewPath} className="text-sm font-medium text-[var(--accent)] underline">
            {t('acceptInvitation.openConversation')}
          </Link>
        </>
      )}
      {state === 'error' && (
        <>
          <XCircle className="h-6 w-6 text-[var(--danger)]" />
          <p role="alert" className="text-sm text-[var(--danger)]">
            {errorMessage}
          </p>
        </>
      )}
    </div>
  )
}
