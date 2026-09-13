import { Search } from 'lucide-react'
import { useState } from 'react'
import { MediaSearchResultCard } from '@/components/chat/MediaSearchResultCard'
import { Button } from '@/components/ui/button'
import { useHealth } from '@/hooks/queries'
import { ApiError, searchMedia } from '@/lib/api'
import type { MediaSearchResult } from '@/lib/types'

/** Standalone media-search page -- independent of the conversational chat
 * flow (`POST /search/media` directly, not `/ask`), per this feature's own
 * requirement. Mirrors `KnowledgeSources.tsx`'s shape: a dedicated page for
 * direct interaction with one source, gated on the server's own
 * `media_search_enabled` capability flag the same way that page gates on a
 * 404 from `/documents`. */
export function MediaSearch() {
  const health = useHealth()
  const [query, setQuery] = useState('')
  const [mediaType, setMediaType] = useState<'image' | 'video' | 'any'>('any')
  const [result, setResult] = useState<MediaSearchResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isSearching, setIsSearching] = useState(false)

  const runSearch = async () => {
    const trimmed = query.trim()
    if (!trimmed) return
    setIsSearching(true)
    setError(null)
    try {
      setResult(await searchMedia(trimmed, mediaType))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Search failed.')
      setResult(null)
    } finally {
      setIsSearching(false)
    }
  }

  if (health.data && !health.data.media_search_enabled) {
    return (
      <div className="h-full min-h-0 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-4 py-6">
          <h1 className="text-xl font-bold">🖼️ Media Search</h1>
          <p className="mt-4 text-sm text-[var(--muted-foreground)]">
            Media search is not yet ready. Make sure your media library is indexed:
          </p>
          <ol className="mt-3 list-inside list-decimal space-y-2 text-sm text-[var(--muted-foreground)]">
            <li>Place images/videos in your configured <code className="rounded bg-[var(--muted)] px-1">MEDIA_LIBRARY_PATH</code> (default: <code className="rounded bg-[var(--muted)] px-1">./media_library</code>)</li>
            <li>Run <code className="rounded bg-[var(--muted)] px-1">python scripts/build_media_index.py</code></li>
            <li>Refresh this page once indexing completes</li>
          </ol>
        </div>
      </div>
    )
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto">
      <div className="mx-auto max-w-4xl px-4 py-6">
        <h1 className="text-xl font-bold">🖼️ Media Search</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Search your local image/video library by describing its content -- no filenames or tags
          needed.
        </p>

        <div className="mt-6 flex flex-col gap-2 sm:flex-row">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void runSearch()
            }}
            placeholder="e.g. the photo of the site inspection"
            className="flex-1 rounded-md border border-[var(--border)] bg-[var(--card)] px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
          />
          <select
            value={mediaType}
            onChange={(event) => setMediaType(event.target.value as 'image' | 'video' | 'any')}
            className="rounded-md border border-[var(--border)] bg-[var(--card)] px-2 py-2 text-sm"
          >
            <option value="any">All media</option>
            <option value="image">Images only</option>
            <option value="video">Videos only</option>
          </select>
          <Button
            variant="primary"
            onClick={() => void runSearch()}
            disabled={isSearching || !query.trim()}
          >
            <Search className="h-4 w-4" />
            {isSearching ? 'Searching…' : 'Search'}
          </Button>
        </div>

        {error && <p className="mt-4 text-sm text-[var(--danger)]">{error}</p>}

        {result && (
          <div className="mt-6 flex flex-col gap-3">
            <p className="text-sm text-[var(--muted-foreground)]">{result.answer}</p>
            <MediaSearchResultCard result={result} />
          </div>
        )}
      </div>
    </div>
  )
}
