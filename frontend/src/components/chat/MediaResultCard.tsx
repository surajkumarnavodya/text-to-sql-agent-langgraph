import { useEffect, useState } from 'react'
import { fetchMediaBlobUrl } from '@/lib/api'
import type { MediaGenerationResult } from '@/lib/types'

/** Renders the "generation" source's contribution -- an actual inline
 * image/video, never a bare link. `result.answer` is already link-free
 * (see agent/orchestrator/nodes.py::generation_node), so a failed/pending
 * result just shows that text; a succeeded one fetches the bytes via
 * GET /media/{media_id} (api/media.py) and renders them directly. */
export function MediaResultCard({ result }: { result: MediaGenerationResult }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (result.status !== 'succeeded' || !result.media_id) return
    let cancelled = false
    let objectUrl: string | null = null

    fetchMediaBlobUrl(result.media_id)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url)
          return
        }
        objectUrl = url
        setBlobUrl(url)
      })
      .catch(() => {
        if (!cancelled) setError('Could not load the generated media.')
      })

    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [result.media_id, result.status])

  if (result.status !== 'succeeded' || !result.media_id) {
    return (
      <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-sm">
        <p className="mb-1 font-semibold">Generated Media</p>
        <p className="text-[var(--muted-foreground)]">{result.answer}</p>
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-sm">
      <p className="mb-2 font-semibold">Generated Media</p>
      {error && <p className="text-[var(--danger)]">{error}</p>}
      {!error && !blobUrl && (
        <p className="text-[var(--muted-foreground)]">Loading generated media...</p>
      )}
      {!error && blobUrl && result.media_type === 'video' && (
        <video src={blobUrl} controls className="max-w-full rounded-md" />
      )}
      {!error && blobUrl && result.media_type !== 'video' && (
        <img src={blobUrl} alt="Generated" className="max-w-full rounded-md" />
      )}
    </div>
  )
}
