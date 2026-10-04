import { AlertTriangle, Database, FileText, Loader2, Lock } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { ChatInput } from '@/components/chat/ChatInput'
import { ChatMessage } from '@/components/chat/ChatMessage'
import { ResultsTable } from '@/components/sql/ResultsTable'
import { Markdown } from '@/components/ui/markdown'
import { ApiError } from '@/lib/api'
import { getSharedAttachmentBlobUrl, getSharedConversation } from '@/lib/shareApi'
import type { ProjectedTurn, SharedConversation as SharedConversationData } from '@/lib/types'
import { useChatStore } from '@/store/chatStore'
import { useLocalAuthStore } from '@/store/localAuthStore'

type ViewState = 'loading' | 'ready' | 'unavailable' | 'rate_limited'

/** The read-only viewer for a shared conversation (`GET /share-view/{ref}`).
 * Reachable without signing in -- it sits outside `AuthGate`/`AppShell` on
 * purpose, so a recipient who isn't signed in still gets a proper page. It is
 * a static snapshot: there is no composer, no "Confirm and Run", and nothing
 * here can start SQL, web, document, model or image work.
 *
 * The turns use the same components as the workspace (ChatMessage, Markdown,
 * ResultsTable), so a shared conversation reads like the original did. The
 * server's projection is the only data source, and it is already filtered. */
export function SharedConversation({ inShell = false }: { inShell?: boolean } = {}) {
  const { ref } = useParams<{ ref: string }>()
  const navigate = useNavigate()
  const { t } = useTranslation()
  const [state, setState] = useState<ViewState>('loading')
  const [data, setData] = useState<SharedConversationData | null>(null)
  const isSignedIn = useLocalAuthStore((store) => store.user !== null)

  useEffect(() => {
    if (!ref) return
    let cancelled = false
    async function load() {
      // Best-effort: a visitor already signed in on this device is recognized
      // as an invited member (the server resolves a share id differently for
      // members). Failing to restore a session never blocks the view attempt.
      try {
        await useLocalAuthStore.getState().initialize()
      } catch {
        // Anonymous visitors simply proceed without an Authorization header.
      }
      try {
        const result = await getSharedConversation(ref!)
        if (!cancelled) {
          setData(result)
          setState('ready')
        }
      } catch (err) {
        if (cancelled) return
        setState(err instanceof ApiError && err.status === 429 ? 'rate_limited' : 'unavailable')
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [ref])

  return (
    // The global `html, body, #root` rules clip overflow (see index.css), so
    // this page is its own scroll region: it fills the viewport and scrolls
    // inside itself, keeping the header pinned and the scrollbar on the right.
    <div className="flex h-full min-h-0 flex-col overflow-y-auto bg-[var(--background)] text-[var(--foreground)]">
      {inShell ? (
        <header className="border-b border-[var(--border)] bg-[var(--background)] px-4 py-3">
          <div className="mx-auto flex max-w-3xl items-center gap-2 text-sm">
            <Lock className="h-3.5 w-3.5 shrink-0 text-[var(--muted-foreground)]" aria-hidden="true" />
            <span className="shrink-0 text-xs font-medium uppercase tracking-wide text-[var(--muted-foreground)]">
              {t('sharedView.badge')}
            </span>
            <h1 className="min-w-0 truncate font-semibold">
              {data?.conversation_title ?? t('sharedView.untitled')}
            </h1>
          </div>
        </header>
      ) : (
        <header className="sticky top-0 z-10 border-b border-[var(--border)] bg-[var(--header)]">
          <div className="mx-auto flex max-w-3xl items-center justify-between gap-3 px-4 py-3">
            <div className="flex min-w-0 items-center gap-2.5">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--accent)] text-[var(--accent-foreground)]">
                <Database className="h-4 w-4" aria-hidden="true" />
              </span>
              <div className="min-w-0">
                <p className="flex items-center gap-1 text-[11px] font-medium uppercase tracking-wide text-[var(--muted-foreground)]">
                  <Lock className="h-3 w-3" aria-hidden="true" />
                  {t('sharedView.badge')}
                </p>
                <h1 className="truncate text-sm font-semibold">
                  {data?.conversation_title ?? t('sharedView.untitled')}
                </h1>
              </div>
            </div>
            {isSignedIn && (
              <Link
                to="/"
                className="shrink-0 rounded-md px-2.5 py-1.5 text-sm font-medium text-[var(--accent)] hover:bg-[var(--accent-soft)]"
              >
                {t('sharedView.openWorkspace')}
              </Link>
            )}
          </div>
        </header>
      )}

      <main className="mx-auto w-full max-w-3xl flex-1 px-4 py-6">
        {data?.snapshot_captured_at && (
          <p className="mb-4 text-xs text-[var(--muted-foreground)]">
            {t('sharedView.snapshotOf', { date: new Date(data.snapshot_captured_at).toLocaleString() })}
          </p>
        )}

        {state === 'loading' && (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-6 w-6 animate-spin text-[var(--muted-foreground)]" aria-label={t('common.loading')} />
          </div>
        )}

        {state === 'rate_limited' && (
          <div role="alert" className="flex flex-col items-center gap-2 py-16 text-center">
            <AlertTriangle className="h-6 w-6 text-[var(--warning)]" aria-hidden="true" />
            <p className="text-sm">{t('sharedView.rateLimited')}</p>
          </div>
        )}

        {state === 'unavailable' && (
          <div role="alert" className="flex flex-col items-center gap-3 py-16 text-center">
            <AlertTriangle className="h-6 w-6 text-[var(--muted-foreground)]" aria-hidden="true" />
            <p className="max-w-md text-sm text-[var(--muted-foreground)]">{t('sharedView.unavailable')}</p>
            {!isSignedIn && (
              <Link
                to="/"
                className="rounded-md bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-foreground)]"
              >
                {t('sharedView.signIn')}
              </Link>
            )}
          </div>
        )}

        {state === 'ready' && data && (
          <div className="flex flex-col gap-6">
            {data.turns.map((turn) => (
              <SharedTurn key={turn.sequence_number} turn={turn} shareRef={ref!} />
            ))}
          </div>
        )}
      </main>

      {inShell && (
        <div className="sticky bottom-0 mx-auto w-full max-w-3xl bg-[var(--background)] px-4 pb-4 pt-2">
          {/* Typing here starts a new chat; the shared copy above stays read-only. */}
          <ChatInput onSend={() => navigate('/')} />
        </div>
      )}

      <footer className="border-t border-[var(--border)]">
        <p className="mx-auto max-w-3xl px-4 py-4 text-center text-xs text-[var(--muted-foreground)]">
          {t('sharedView.readOnlyNote')}
        </p>
      </footer>
    </div>
  )
}

function SharedTurn({ turn, shareRef }: { turn: ProjectedTurn; shareRef: string }) {
  const { t } = useTranslation()

  if (turn.role === 'user') {
    return (
      <div className="flex justify-end">
        <ChatMessage message={{ role: 'user', content: turn.content }} />
      </div>
    )
  }

  const answer = turn.insight ?? turn.synthesized_answer ?? turn.content
  const columns = Array.isArray(turn.result_snapshot?.columns) ? (turn.result_snapshot?.columns as string[]) : []
  const rows = Array.isArray(turn.result_snapshot?.rows) ? (turn.result_snapshot?.rows as unknown[][]) : []
  const truncated = turn.result_snapshot?.truncated === true

  return (
    <div className="flex gap-2.5">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--muted)] text-[var(--muted-foreground)]">
        <Database className="h-4 w-4" aria-hidden="true" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-3 rounded-2xl border border-[var(--border)] bg-[var(--card)] p-4">
        <Markdown>{answer}</Markdown>

        {turn.sources_used.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {turn.sources_used.map((source) => (
              <span key={source} className="rounded-full bg-[var(--muted)] px-2 py-0.5 text-xs text-[var(--muted-foreground)]">
                {source}
              </span>
            ))}
          </div>
        )}

        {turn.sql && (
          <pre className="overflow-x-auto rounded-md bg-[var(--muted)] p-3 font-mono text-xs leading-relaxed">
            <code>{turn.sql}</code>
          </pre>
        )}

        {columns.length > 0 && (
          <div className="flex flex-col gap-1">
            <ResultsTable columns={columns} rows={rows} />
            {truncated && <p className="text-xs text-[var(--muted-foreground)]">{t('sharedView.resultTruncated')}</p>}
          </div>
        )}

        {turn.attachment_refs.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {turn.attachment_refs.map((attachment) => (
              <SharedAttachmentChip
                key={attachment.attachment_id}
                shareRef={shareRef}
                attachmentId={attachment.attachment_id}
                filename={attachment.filename}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function SharedAttachmentChip({
  shareRef,
  attachmentId,
  filename,
}: {
  shareRef: string
  attachmentId: string
  filename: string
}) {
  const { t } = useTranslation()
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    return () => {
      if (blobUrl) URL.revokeObjectURL(blobUrl)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- cleanup only, intentionally not re-run on blobUrl change
  }, [])

  const handleOpen = async () => {
    if (blobUrl) {
      window.open(blobUrl, '_blank', 'noopener,noreferrer')
      return
    }
    try {
      const url = await getSharedAttachmentBlobUrl(shareRef, attachmentId)
      setBlobUrl(url)
      window.open(url, '_blank', 'noopener,noreferrer')
    } catch {
      setError(true)
    }
  }

  return (
    <button
      type="button"
      onClick={() => void handleOpen()}
      className="flex items-center gap-1.5 rounded-md border border-[var(--border)] px-2 py-1 text-xs hover:bg-[var(--muted)]"
    >
      <FileText className="h-3.5 w-3.5" aria-hidden="true" />
      <span className="max-w-[10rem] truncate">{filename}</span>
      {error && <span className="text-[var(--danger)]">{t('sharedView.attachmentUnavailable')}</span>}
    </button>
  )
}

/** The route entry. A signed-in viewer gets the normal app shell (their
 * history, the AI Workspace menu, a new-chat composer) around the shared copy;
 * an anonymous visitor gets the standalone read-only page. */
export function SharedEntry() {
  const userId = useLocalAuthStore((store) => store.user?.id ?? null)
  // AuthGate normally loads a signed-in user's chat history. This route sits
  // outside AuthGate, so without this the sidebar history would wait forever.
  useEffect(() => {
    if (userId) void useChatStore.getState().hydrateHistoryFromServer()
  }, [userId])

  if (userId) {
    return (
      <AppShell>
        <SharedConversation inShell />
      </AppShell>
    )
  }
  return <SharedConversation />
}
