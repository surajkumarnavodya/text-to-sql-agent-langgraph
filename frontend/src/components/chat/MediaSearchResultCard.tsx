import { useEffect, useState } from 'react'
import { fetchLibraryMediaBlobUrl } from '@/lib/api'
import type { MediaSearchHit, MediaSearchResult } from '@/lib/types'

function formatTimestamp(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds))
  const minutes = Math.floor(whole / 60)
  const secs = whole % 60
  return `${minutes}:${secs.toString().padStart(2, '0')}`
}

/** One retrieved image or video-segment thumbnail -- fetches its own blob
 * URL independently (mirrors `MediaResultCard.tsx`'s loading/error/
 * cleanup pattern, applied per-hit here instead of per-whole-result since
 * a search can return several hits at once). */
function MediaHitThumbnail({ hit }: { hit: MediaSearchHit }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    let cancelled = false
    let objectUrl: string | null = null

    fetchLibraryMediaBlobUrl(hit.media_id)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url)
          return
        }
        objectUrl = url
        setBlobUrl(url)
      })
      .catch(() => {
        if (!cancelled) setError(true)
      })

    return () => {
      cancelled = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [hit.media_id])

  return (
    <div className="flex flex-col gap-1">
      <div className="relative aspect-video overflow-hidden rounded-md bg-[var(--muted)]">
        {error && (
          <div className="flex h-full items-center justify-center text-xs text-[var(--muted-foreground)]">
            Could not load
          </div>
        )}
        {!error && !blobUrl && (
          <div className="flex h-full items-center justify-center text-xs text-[var(--muted-foreground)]">
            Loading...
          </div>
        )}
        {!error && blobUrl && (
          <img src={blobUrl} alt={hit.caption} className="h-full w-full object-cover" />
        )}
        {hit.media_type === 'video' && hit.timestamp_start !== null && hit.timestamp_end !== null && (
          <span className="absolute bottom-1 right-1 rounded bg-black/70 px-1.5 py-0.5 text-[11px] text-white">
            {formatTimestamp(hit.timestamp_start)}-{formatTimestamp(hit.timestamp_end)}
          </span>
        )}
      </div>
      <p className="line-clamp-2 text-xs text-[var(--muted-foreground)]">{hit.caption}</p>
    </div>
  )
}

/** Renders the "media_search" source's contribution -- a thumbnail grid
 * of retrieved images/video segments. Rendered unconditionally by
 * `SourcesUsedPanel.tsx` whenever `media_search_result.hits` is
 * non-empty, the same "always outside the synthesis-text ternary" rule
 * `generation_result`'s `MediaResultCard` already established -- the
 * hits are a structured citation, never text-parsed out of the answer. */
export function MediaSearchResultCard({ result }: { result: MediaSearchResult }) {
  if (result.hits.length === 0) return null

  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-sm">
      <p className="mb-2 font-semibold">Media Library</p>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {result.hits.map((hit) => (
          <MediaHitThumbnail key={hit.media_id} hit={hit} />
        ))}
      </div>
    </div>
  )
}
