import { AlertTriangle, Loader2, Trash2, UserMinus } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/ui/copy-button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useHealth } from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import {
  createShare,
  getShare,
  inviteShareMember,
  regenerateShareLink,
  removeShareMember,
  revokeShare,
  updateShare,
} from '@/lib/shareApi'
import type { Share, ShareAccessMode } from '@/lib/types'

export interface ShareModalProps {
  conversationId: string
  conversationTitle: string
  onClose: () => void
}

type LoadState = 'loading' | 'not_shared' | 'ready' | 'error'

/** Owner-facing share dialog, kept deliberately small: who can open the
 * conversation, one link to copy, invite by email, and a footer for the
 * snapshot and stopping the share. Every action still calls the same server
 * route, and the server re-checks ownership on each one -- nothing here is the
 * real boundary.
 *
 * "Anyone with the link" is offered only when the server allows public links
 * (`share_public_links_enabled` on /health). Otherwise the server would refuse
 * it, so the dialog shows why instead of a generic error. */
export function ShareModal({ conversationId, conversationTitle, onClose }: ShareModalProps) {
  const { t } = useTranslation()
  const health = useHealth()
  const publicLinksEnabled = health.data?.share_public_links_enabled === true
  const [state, setState] = useState<LoadState>('loading')
  const [share, setShare] = useState<Share | null>(null)
  const [error, setError] = useState<string | null>(null)
  // The raw "anyone with the link" token is only ever in a response once --
  // this holds that one-time path until the dialog closes or a new link is made.
  const [oneTimeLinkPath, setOneTimeLinkPath] = useState<string | null>(null)
  const [oneTimeInvitePath, setOneTimeInvitePath] = useState<string | null>(null)
  const [confirmWiden, setConfirmWiden] = useState(false)
  const [confirmRevoke, setConfirmRevoke] = useState(false)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteError, setInviteError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const result = await getShare(conversationId)
        if (!cancelled) {
          setShare(result)
          setState('ready')
        }
      } catch (err) {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 404) {
          setState('not_shared')
        } else {
          setError(t('share.loadError'))
          setState('error')
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [conversationId, t])

  const origin = typeof window !== 'undefined' ? window.location.origin : ''

  // The message to show for a failed action: the server's own reason for a
  // refusal (a 4xx with a readable message), otherwise a generic line.
  const messageFor = (err: unknown, fallback: string): string => {
    if (err instanceof ApiError && err.status >= 400 && err.status < 500 && err.message && !err.message.includes('[object')) {
      return err.message
    }
    return fallback
  }

  const run = async (action: string, fallback: string, task: () => Promise<void>) => {
    setBusyAction(action)
    setError(null)
    try {
      await task()
    } catch (err) {
      setError(messageFor(err, fallback))
    } finally {
      setBusyAction(null)
    }
  }

  const handleEnableSharing = () =>
    run('enable', t('share.actionError'), async () => {
      const result = await createShare(conversationId, { access_mode: 'invite_only' })
      setShare(result.share)
      setState('ready')
    })

  const applyAccessMode = (mode: ShareAccessMode) =>
    run('access_mode', t('share.actionError'), async () => {
      if (!share) return
      let updated = await updateShare(conversationId, { version: share.version, access_mode: mode })
      if (mode === 'anyone_with_link' && !updated.link_available) {
        const regenerated = await regenerateShareLink(conversationId)
        updated = regenerated.share
        setOneTimeLinkPath(regenerated.link?.view_path ?? null)
      }
      setShare(updated)
      setConfirmWiden(false)
    })

  const handleAccessChange = (mode: ShareAccessMode) => {
    if (mode === share?.access_mode) return
    if (mode === 'anyone_with_link') {
      setConfirmWiden(true)
      return
    }
    void applyAccessMode(mode)
  }

  const handleNewLink = () =>
    run('regenerate', t('share.actionError'), async () => {
      const result = await regenerateShareLink(conversationId)
      setShare(result.share)
      setOneTimeLinkPath(result.link?.view_path ?? null)
    })

  const handleRefreshSnapshot = () =>
    run('refresh_snapshot', t('share.actionError'), async () => {
      if (!share) return
      setShare(await updateShare(conversationId, { version: share.version, refresh_snapshot: true }))
    })

  const handleToggleActive = () =>
    run('toggle_status', t('share.actionError'), async () => {
      if (!share) return
      const nextStatus = share.status === 'active' ? 'disabled' : 'active'
      setShare(await updateShare(conversationId, { version: share.version, status: nextStatus }))
    })

  const handleStopSharing = () =>
    run('revoke', t('share.actionError'), async () => {
      if (!share) return
      await revokeShare(conversationId, share.version)
      setShare(null)
      setOneTimeLinkPath(null)
      setOneTimeInvitePath(null)
      setConfirmRevoke(false)
      setState('not_shared')
    })

  const handleInvite = async () => {
    const email = inviteEmail.trim()
    if (!email) return
    setBusyAction('invite')
    setInviteError(null)
    setOneTimeInvitePath(null)
    try {
      const result = await inviteShareMember(conversationId, { email })
      setOneTimeInvitePath(result.invitation_path)
      setInviteEmail('')
      setShare(await getShare(conversationId))
    } catch (err) {
      setInviteError(messageFor(err, t('share.inviteError')))
    } finally {
      setBusyAction(null)
    }
  }

  const handleRemoveMember = (memberId: string) =>
    run(`remove_${memberId}`, t('share.actionError'), async () => {
      setShare(await removeShareMember(conversationId, memberId))
    })

  // The link to copy: a fresh one-time link when "anyone with the link" is
  // on, otherwise the invite-only link that invited people open after signing in.
  const linkText =
    share?.access_mode === 'anyone_with_link'
      ? oneTimeLinkPath
        ? origin + oneTimeLinkPath
        : null
      : share
        ? `${origin}/shared/${share.id}`
        : null

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t('share.title')}</DialogTitle>
          <DialogDescription className="truncate">{conversationTitle}</DialogDescription>
        </DialogHeader>

        {state === 'loading' && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-[var(--muted-foreground)]" />
          </div>
        )}

        {state === 'error' && (
          <p role="alert" className="text-sm text-[var(--danger)]">
            {error ?? t('share.loadError')}
          </p>
        )}

        {state === 'not_shared' && (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-[var(--muted-foreground)]">{t('share.notSharedDescription')}</p>
            {error && (
              <p role="alert" className="text-sm text-[var(--danger)]">
                {error}
              </p>
            )}
            <Button variant="primary" onClick={() => void handleEnableSharing()} disabled={busyAction === 'enable'}>
              {busyAction === 'enable' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {t('share.enableSharing')}
            </Button>
          </div>
        )}

        {state === 'ready' && share && (
          <div className="flex flex-col gap-5">
            {error && (
              <p role="alert" className="flex items-start gap-2 rounded-md bg-[var(--danger)]/10 px-3 py-2 text-sm text-[var(--danger)]">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                {error}
              </p>
            )}

            {/* 1. Who can open it */}
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-[var(--muted-foreground)]">
                {t('share.generalAccess')}
              </legend>
              <label className="flex cursor-pointer items-start gap-2.5 rounded-md border border-[var(--border)] p-3 text-sm has-[:checked]:border-[var(--accent)] has-[:checked]:bg-[var(--accent-soft)]">
                <input
                  type="radio"
                  name="access-mode"
                  className="mt-0.5"
                  checked={share.access_mode === 'invite_only'}
                  onChange={() => handleAccessChange('invite_only')}
                />
                <span className="flex flex-col">
                  <span className="font-medium">{t('share.accessInviteOnly')}</span>
                  <span className="text-xs text-[var(--muted-foreground)]">{t('share.accessInviteOnlyHint')}</span>
                </span>
              </label>
              <label
                className={`flex items-start gap-2.5 rounded-md border border-[var(--border)] p-3 text-sm has-[:checked]:border-[var(--accent)] has-[:checked]:bg-[var(--accent-soft)] ${
                  publicLinksEnabled || share.access_mode === 'anyone_with_link' ? 'cursor-pointer' : 'opacity-60'
                }`}
              >
                <input
                  type="radio"
                  name="access-mode"
                  className="mt-0.5"
                  checked={share.access_mode === 'anyone_with_link'}
                  disabled={!publicLinksEnabled && share.access_mode !== 'anyone_with_link'}
                  onChange={() => handleAccessChange('anyone_with_link')}
                />
                <span className="flex flex-col">
                  <span className="font-medium">{t('share.accessAnyoneWithLink')}</span>
                  <span className="text-xs text-[var(--muted-foreground)]">
                    {publicLinksEnabled ? t('share.accessAnyoneWithLinkHint') : t('share.publicLinksOff')}
                  </span>
                </span>
              </label>
              {confirmWiden && (
                <div className="flex flex-col gap-2 rounded-md border border-[var(--warning)] p-3 text-xs">
                  <span className="flex items-center gap-1.5 text-[var(--warning)]">
                    <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
                    {t('share.widenAccessWarning')}
                  </span>
                  <div className="flex justify-end gap-2">
                    <Button size="sm" variant="ghost" onClick={() => setConfirmWiden(false)}>
                      {t('common.cancel')}
                    </Button>
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => void applyAccessMode('anyone_with_link')}
                      disabled={busyAction === 'access_mode'}
                    >
                      {t('common.confirm')}
                    </Button>
                  </div>
                </div>
              )}
            </fieldset>

            {/* 2. The one link to copy */}
            <div className="flex flex-col gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted-foreground)]">
                {t('share.linkLabel')}
              </span>
              {linkText ? (
                <div className="flex items-center gap-2 rounded-md border border-[var(--border)] bg-[var(--muted)] py-1.5 pl-3 pr-1.5">
                  <span className="min-w-0 flex-1 truncate font-mono text-xs">{linkText}</span>
                  <CopyButton getText={() => linkText} label={t('share.copyLink')} />
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  <p className="text-xs text-[var(--muted-foreground)]">{t('share.regenerateNote')}</p>
                  <Button size="sm" variant="secondary" onClick={() => void handleNewLink()} disabled={busyAction === 'regenerate'} className="self-start">
                    {t('share.regenerateLink')}
                  </Button>
                </div>
              )}
              {oneTimeLinkPath && <p className="text-xs text-[var(--muted-foreground)]">{t('share.oneTimeLinkNotice')}</p>}
            </div>

            {/* 3. Invite by email */}
            <div className="flex flex-col gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted-foreground)]">
                {t('share.inviteLabel')}
              </span>
              <form
                className="flex gap-2"
                onSubmit={(event) => {
                  event.preventDefault()
                  void handleInvite()
                }}
              >
                <input
                  type="email"
                  value={inviteEmail}
                  onChange={(event) => setInviteEmail(event.target.value)}
                  placeholder={t('share.invitePlaceholder')}
                  aria-label={t('share.invitePlaceholder')}
                  className="h-9 min-w-0 flex-1 rounded-md border border-[var(--border)] bg-[var(--input)] px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
                />
                <Button
                  type="submit"
                  size="sm"
                  variant="secondary"
                  disabled={busyAction === 'invite' || !inviteEmail.trim()}
                >
                  {t('share.inviteAction')}
                </Button>
              </form>
              {inviteError && <p className="text-xs text-[var(--danger)]">{inviteError}</p>}
              {oneTimeInvitePath && (
                <div className="flex items-center gap-2 rounded-md border border-[var(--border)] bg-[var(--muted)] py-1 pl-3 pr-1 text-xs">
                  <span className="min-w-0 flex-1 truncate font-mono">{origin + oneTimeInvitePath}</span>
                  <CopyButton getText={() => origin + oneTimeInvitePath} label={t('share.copyInvite')} />
                </div>
              )}
            </div>

            {/* 4. People with access */}
            {share.members.length > 0 && (
              <div className="flex flex-col gap-1">
                <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted-foreground)]">
                  {t('share.peopleWithAccess')}
                </span>
                <ul className="flex flex-col">
                  {share.members.map((member) => (
                    <li key={member.id} className="flex items-center justify-between gap-2 py-1 text-sm">
                      <span className="min-w-0 truncate">
                        {member.display_name ?? member.invited_email ?? t('share.unknownMember')}
                        <span className="ml-1.5 text-xs text-[var(--muted-foreground)]">
                          ({t(`share.memberStatus.${member.status}`)})
                        </span>
                      </span>
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={t('share.removeMember')}
                        onClick={() => void handleRemoveMember(member.id)}
                        disabled={busyAction === `remove_${member.id}`}
                      >
                        <UserMinus className="h-3.5 w-3.5" />
                      </Button>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* 5. Footer: snapshot, pause, stop */}
            <div className="flex flex-col gap-3 border-t border-[var(--border)] pt-4">
              <p className="text-xs text-[var(--muted-foreground)]">
                {share.snapshot_captured_at
                  ? t('share.snapshotNotice', { date: new Date(share.snapshot_captured_at).toLocaleString() })
                  : t('share.snapshotNoticeUnknown')}
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" variant="secondary" onClick={() => void handleRefreshSnapshot()} disabled={busyAction === 'refresh_snapshot'}>
                  {t('share.refreshSnapshot')}
                </Button>
                <Button size="sm" variant="secondary" onClick={() => void handleToggleActive()} disabled={busyAction === 'toggle_status'}>
                  {share.status === 'active' ? t('share.turnOff') : t('share.turnOn')}
                </Button>
              </div>
              {!confirmRevoke ? (
                <Button
                  size="sm"
                  variant="ghost"
                  className="justify-start text-[var(--danger)]"
                  onClick={() => setConfirmRevoke(true)}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  {t('share.revoke')}
                </Button>
              ) : (
                <div className="flex flex-col gap-2 rounded-md border border-[var(--danger)] p-3 text-xs">
                  <span>{t('share.revokeWarning')}</span>
                  <div className="flex justify-end gap-2">
                    <Button size="sm" variant="ghost" onClick={() => setConfirmRevoke(false)}>
                      {t('common.cancel')}
                    </Button>
                    <Button size="sm" variant="danger" onClick={() => void handleStopSharing()} disabled={busyAction === 'revoke'}>
                      {t('share.revoke')}
                    </Button>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
