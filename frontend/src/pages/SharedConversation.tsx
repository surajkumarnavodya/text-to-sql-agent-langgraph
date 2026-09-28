import { AlertTriangle, FileText, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'
import { Markdown } from '@/components/ui/markdown'
import { ApiError } from '@/lib/api'
import { getSharedAttachmentBlobUrl, getSharedConversation } from '@/lib/shareApi'
import type { ProjectedTurn, SharedConversation as SharedConversationData } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'

type ViewState = 'loading' | 'ready' | 'unavailable' | 'rate_limited'

/** The read-only projection viewer for a shared conversation
 * (`GET /share-view/{ref}`, this page's own route is `/shared/:ref`, kept
 * deliberately distinct on the backend -- see `api/shares.py`'s module
 * docstring). Reachable by **anyone with the link**, with no sign-in
 * required, and deliberately sits outside `AuthGate`/`AppShell` -- it is
 * not "the app," it is a single static snapshot.
 *
 * There is no composer, no "Confirm and Run," no chart re-generation, and
 * no way to trigger SQL/web/document/vector/model/image work of any kind
 * from this page -- everything rendered here is a plain, already-computed
 * field off the server's own projection response. */
export function SharedConversation() {
  const { ref } = useParams<{ ref: string }>()
  const { t } = useTranslation()
  const [state, setState] = useState<ViewState>('loading')
  const [data, setData] = useState<SharedConversationData | null>(null)

  useEffect(() => {
    if (!ref) return
    let cancelled = false
    async function load() {
      // Best-effort: if the visitor already has a live local session (they
      // opened this link in a tab where they're signed in), this populates
      // the in-memory bearer token before the fetch below so the backend
      // can recognize them as an invited member rather than an anonymous
      // link visitor. An anonymous visitor with no session simply settles
      // into "unconfigured"/"unauthenticated" here and the fetch proceeds
      // with no Authorization header -- still correct for a public link.
      try {
        await useLocalAuthStore.getState().initialize()
      } catch {
        // Never blocks the actual view attempt below.
      }
      try {
        const result = await getSharedConversation(ref!)
        if (!cancelled) {
          setData(result)
          setState('ready')
        }
      } catch (err) {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 429) {
          setState('rate_limited')
        } else {
          setState('unavailable')
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [ref])

  return (
    <div className="mx-auto flex min-h-screen max-w-3xl flex-col gap-4 bg-[var(--background)] px-4 py-8 text-[var(--foreground)]">
      <header className="border-b border-[var(--border)] pb-4">
        <p className="text-xs font-semibold uppercase tracking-wide text-[var(--muted-foreground)]">
          {t('sharedView.badge')}
        </p>
        <h1 className="mt-1 truncate text-lg font-semibold">
          {data?.conversation_title ?? t('sharedView.untitled')}
        </h1>
        {data?.snapshot_captured_at && (
          <p className="mt-1 text-xs text-[var(--muted-foreground)]">
            {t('sharedView.snapshotOf', { date: new Date(data.snapshot_captured_at).toLocaleString() })}
          </p>
        )}
      </header>

      {state === 'loading' && (
        <div className="flex flex-1 items-center justify-center py-16">
          <Loader2 className="h-6 w-6 animate-spin text-[var(--muted-foreground)]" />
        </div>
      )}

      {state === 'rate_limited' && (
        <div role="alert" className="flex flex-1 flex-col items-center justify-center gap-2 py-16 text-center">
          <AlertTriangle className="h-6 w-6 text-[var(--warning)]" />
          <p className="text-sm">{t('sharedView.rateLimited')}</p>
        </div>
      )}

      {state === 'unavailable' && (
        <div role="alert" className="flex flex-1 flex-col items-center justify-center gap-2 py-16 text-center">
          <AlertTriangle className="h-6 w-6 text-[var(--muted-foreground)]" />
          <p className="text-sm text-[var(--muted-foreground)]">{t('sharedView.unavailable')}</p>
        </div>
      )}

      {state === 'ready' && data && (
        <div className="flex flex-col gap-6">
          {data.turns.map((turn) => (
            <SharedTurn key={turn.sequence_number} turn={turn} shareRef={ref!} />
          ))}
        </div>
      )}
    </div>
  )
}

function SharedTurn({ turn, shareRef }: { turn: ProjectedTurn; shareRef: string }) {
  if (turn.role === 'user') {
    return (
      <div className="self-end rounded-lg bg-[var(--accent-soft)] px-4 py-2 text-sm">{turn.content}</div>
    )
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] p-4">
      <Markdown>{turn.insight ?? turn.synthesized_answer ?? turn.content}</Markdown>

      {turn.sources_used.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {turn.sources_used.map((source) => (
            <span
              key={source}
              className="rounded-full bg-[var(--muted)] px-2 py-0.5 text-xs text-[var(--muted-foreground)]"
            >
              {source}
            </span>
          ))}
        </div>
      )}

      {turn.sql && (
        <pre className="overflow-x-auto rounded-md bg-[var(--muted)] p-3 text-xs">
          <code>{turn.sql}</code>
        </pre>
      )}

      {turn.result_snapshot && (
        <ResultSnapshotTable snapshot={turn.result_snapshot} />
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
  )
}

/** A bounded, read-only render of an already-confirmed result snapshot --
 * never an interactive chart, and never anything that could re-issue the
 * query: `columns`/`rows` are exactly what the owner's own "Confirm and
 * Run" produced at share time, nothing here calls `/execute`. */
function ResultSnapshotTable({ snapshot }: { snapshot: Record<string, unknown> }) {
  const { t } = useTranslation()
  const columns = Array.isArray(snapshot.columns) ? (snapshot.columns as string[]) : []
  const rows = Array.isArray(snapshot.rows) ? (snapshot.rows as unknown[][]) : []
  if (columns.length === 0) return null

  return (
    <div className="overflow-x-auto rounded-md border border-[var(--border)]">
      <table className="w-full text-left text-xs">
        <thead>
          <tr>
            {columns.map((col) => (
              <th key={col} className="border-b border-[var(--border)] px-2 py-1 font-medium">
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 50).map((row, rowIndex) => (
            // eslint-disable-next-line react/no-array-index-key -- a static, never-reordered snapshot
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => (
                // eslint-disable-next-line react/no-array-index-key -- see above
                <td key={cellIndex} className="border-b border-[var(--border)] px-2 py-1">
                  {String(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {typeof snapshot.truncated === 'boolean' && snapshot.truncated && (
        <p className="px-2 py-1 text-[10px] text-[var(--muted-foreground)]">{t('sharedView.resultTruncated')}</p>
      )}
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
      <FileText className="h-3.5 w-3.5" />
      <span className="max-w-[10rem] truncate">{filename}</span>
      {error && <span className="text-[var(--danger)]">{t('sharedView.attachmentUnavailable')}</span>}
    </button>
  )
}
