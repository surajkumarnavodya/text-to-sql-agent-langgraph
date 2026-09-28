import { AlertTriangle, Link2, Loader2, Trash2, UserMinus } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/ui/copy-button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
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

/** Owner-facing share management dialog -- create/update/revoke a
 * conversation's share, invite/remove members, regenerate the "anyone with
 * the link" token. Never shown to anyone but the conversation's own owner
 * (the trigger is only rendered from `ConversationListItem`'s own per-item
 * menu, itself only reachable for a conversation already in *this* user's
 * own list) -- but every action here still hits a server route that
 * independently re-derives and checks ownership, exactly like every other
 * mutating action in this app (see `api/shares.py`'s own docstring). A
 * frontend-only restriction is never treated as the real boundary. */
export function ShareModal({ conversationId, conversationTitle, onClose }: ShareModalProps) {
  const { t } = useTranslation()
  const [state, setState] = useState<LoadState>('loading')
  const [share, setShare] = useState<Share | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [oneTimeLinkPath, setOneTimeLinkPath] = useState<string | null>(null)
  const [confirmWiden, setConfirmWiden] = useState(false)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteError, setInviteError] = useState<string | null>(null)
  const [oneTimeInvitePath, setOneTimeInvitePath] = useState<string | null>(null)
  const [confirmRevoke, setConfirmRevoke] = useState(false)

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

  const handleEnableSharing = async () => {
    setBusyAction('enable')
    setError(null)
    try {
      const result = await createShare(conversationId, { access_mode: 'invite_only' })
      setShare(result.share)
      setState('ready')
    } catch {
      setError(t('share.actionError'))
    } finally {
      setBusyAction(null)
    }
  }

  const applyAccessMode = async (mode: ShareAccessMode) => {
    if (!share) return
    setBusyAction('access_mode')
    setError(null)
    try {
      let updated = await updateShare(conversationId, { version: share.version, access_mode: mode })
      if (mode === 'anyone_with_link' && !updated.link_available) {
        const regenerated = await regenerateShareLink(conversationId)
        updated = regenerated.share
        setOneTimeLinkPath(regenerated.link?.view_path ?? null)
      }
      setShare(updated)
      setConfirmWiden(false)
    } catch {
      setError(t('share.actionError'))
    } finally {
      setBusyAction(null)
    }
  }

  const handleAccessModeChange = (mode: ShareAccessMode) => {
    if (mode === 'anyone_with_link') {
      setConfirmWiden(true)
      return
    }
    void applyAccessMode(mode)
  }

  const handleRegenerateLink = async () => {
    setBusyAction('regenerate')
    setError(null)
    try {
      const result = await regenerateShareLink(conversationId)
      setShare(result.share)
      setOneTimeLinkPath(result.link?.view_path ?? null)
    } catch {
      setError(t('share.actionError'))
    } finally {
      setBusyAction(null)
    }
  }

  const handleRefreshSnapshot = async () => {
    if (!share) return
    setBusyAction('refresh_snapshot')
    setError(null)
    try {
      const updated = await updateShare(conversationId, { version: share.version, refresh_snapshot: true })
      setShare(updated)
    } catch {
      setError(t('share.actionError'))
    } finally {
      setBusyAction(null)
    }
  }

  const handleToggleActive = async () => {
    if (!share) return
    setBusyAction('toggle_status')
    setError(null)
    try {
      const nextStatus = share.status === 'active' ? 'disabled' : 'active'
      const updated = await updateShare(conversationId, { version: share.version, status: nextStatus })
      setShare(updated)
    } catch {
      setError(t('share.actionError'))
    } finally {
      setBusyAction(null)
    }
  }

  const handleRevoke = async () => {
    if (!share) return
    setBusyAction('revoke')
    setError(null)
    try {
      await revokeShare(conversationId, share.version)
      setShare(null)
      setState('not_shared')
      setConfirmRevoke(false)
    } catch {
      setError(t('share.actionError'))
    } finally {
      setBusyAction(null)
    }
  }

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
      const refreshed = await getShare(conversationId)
      setShare(refreshed)
    } catch {
      setInviteError(t('share.inviteError'))
    } finally {
      setBusyAction(null)
    }
  }

  const handleRemoveMember = async (memberId: string) => {
    setBusyAction(`remove_${memberId}`)
    setError(null)
    try {
      const updated = await removeShareMember(conversationId, memberId)
      setShare(updated)
    } catch {
      setError(t('share.actionError'))
    } finally {
      setBusyAction(null)
    }
  }

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
            <Button variant="primary" onClick={() => void handleEnableSharing()} disabled={busyAction === 'enable'}>
              {busyAction === 'enable' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {t('share.enableSharing')}
            </Button>
          </div>
        )}

        {state === 'ready' && share && (
          <div className="flex flex-col gap-5">
            <p className="rounded-md bg-[var(--muted)] px-3 py-2 text-xs text-[var(--muted-foreground)]">
              {share.snapshot_captured_at
                ? t('share.snapshotNotice', {
                    date: new Date(share.snapshot_captured_at).toLocaleString(),
                  })
                : t('share.snapshotNoticeUnknown')}
            </p>

            {error && (
              <p role="alert" className="text-sm text-[var(--danger)]">
                {error}
              </p>
            )}

            {/* General access */}
            <div className="flex flex-col gap-2">
              <span className="text-xs font-semibold uppercase text-[var(--muted-foreground)]">
                {t('share.generalAccess')}
              </span>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="access-mode"
                  checked={share.access_mode === 'invite_only'}
                  onChange={() => handleAccessModeChange('invite_only')}
                />
                {t('share.accessInviteOnly')}
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="access-mode"
                  checked={share.access_mode === 'anyone_with_link'}
                  onChange={() => handleAccessModeChange('anyone_with_link')}
                />
                {t('share.accessAnyoneWithLink')}
              </label>
              {confirmWiden && (
                <div className="flex flex-col gap-2 rounded-md border border-[var(--warning)] bg-[var(--muted)] p-2 text-xs">
                  <span className="flex items-center gap-1.5 text-[var(--warning)]">
                    <AlertTriangle className="h-3.5 w-3.5" />
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
              {share.access_mode === 'anyone_with_link' && (
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => void handleRegenerateLink()}
                    disabled={busyAction === 'regenerate'}
                  >
                    <Link2 className="h-3.5 w-3.5" />
                    {t('share.regenerateLink')}
                  </Button>
                  {oneTimeLinkPath && (
                    <span className="flex min-w-0 items-center gap-1 rounded border border-[var(--border)] px-2 py-1 text-xs">
                      <span className="max-w-[12rem] truncate">{origin + oneTimeLinkPath}</span>
                      <CopyButton
                        getText={() => origin + oneTimeLinkPath}
                        label={t('share.copyLink')}
                      />
                    </span>
                  )}
                </div>
              )}
              {oneTimeLinkPath && (
                <p className="text-xs text-[var(--muted-foreground)]">{t('share.oneTimeLinkNotice')}</p>
              )}
            </div>

            {/* Expiry */}
            <div className="flex items-center justify-between gap-2 text-sm">
              <span>{t('share.expiresLabel')}</span>
              <span className="text-[var(--muted-foreground)]">
                {share.expires_at ? new Date(share.expires_at).toLocaleDateString() : t('share.noExpiry')}
              </span>
            </div>

            {/* Invite */}
            <div className="flex flex-col gap-2">
              <span className="text-xs font-semibold uppercase text-[var(--muted-foreground)]">
                {t('share.inviteLabel')}
              </span>
              <div className="flex gap-2">
                <input
                  type="email"
                  value={inviteEmail}
                  onChange={(event) => setInviteEmail(event.target.value)}
                  placeholder={t('share.invitePlaceholder')}
                  className="h-8 flex-1 rounded border border-[var(--border)] bg-[var(--input)] px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
                />
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => void handleInvite()}
                  disabled={busyAction === 'invite' || !inviteEmail.trim()}
                >
                  {t('share.inviteAction')}
                </Button>
              </div>
              {inviteError && <p className="text-xs text-[var(--danger)]">{inviteError}</p>}
              {oneTimeInvitePath && (
                <div className="flex items-center gap-1 rounded border border-[var(--border)] px-2 py-1 text-xs">
                  <span className="max-w-[14rem] truncate">{origin + oneTimeInvitePath}</span>
                  <CopyButton getText={() => origin + oneTimeInvitePath} label={t('share.copyInvite')} />
                </div>
              )}
            </div>

            {/* People with access */}
            {share.members.length > 0 && (
              <div className="flex flex-col gap-1">
                <span className="text-xs font-semibold uppercase text-[var(--muted-foreground)]">
                  {t('share.peopleWithAccess')}
                </span>
                <ul className="flex flex-col gap-1">
                  {share.members.map((member) => (
                    <li key={member.id} className="flex items-center justify-between gap-2 text-sm">
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

            {/* Danger zone */}
            <div className="flex flex-col gap-2 border-t border-[var(--border)] pt-3">
              <div className="flex items-center justify-between text-sm">
                <span>{t('share.sharingActive')}</span>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => void handleToggleActive()}
                  disabled={busyAction === 'toggle_status'}
                >
                  {share.status === 'active' ? t('share.turnOff') : t('share.turnOn')}
                </Button>
              </div>
              <Button size="sm" variant="secondary" onClick={() => void handleRefreshSnapshot()} disabled={busyAction === 'refresh_snapshot'}>
                {t('share.refreshSnapshot')}
              </Button>
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
                <div className="flex flex-col gap-2 rounded-md border border-[var(--danger)] p-2 text-xs">
                  <span>{t('share.revokeWarning')}</span>
                  <div className="flex justify-end gap-2">
                    <Button size="sm" variant="ghost" onClick={() => setConfirmRevoke(false)}>
                      {t('common.cancel')}
                    </Button>
                    <Button size="sm" variant="danger" onClick={() => void handleRevoke()} disabled={busyAction === 'revoke'}>
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
