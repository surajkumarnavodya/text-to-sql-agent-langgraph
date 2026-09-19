import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError } from '@/lib/api'
import { searchChatHistory } from '@/lib/identityApi'
import type { SearchHit } from '@/lib/types'
import { useLocalAuthStore } from '@/store/localAuthStore'

const SEARCH_DEBOUNCE_MS = 300

export interface ChatSearchState {
  query: string
  setQuery: (value: string) => void
  clear: () => void
  /** True once server-side search across the user's complete history is
   * active for this session (a locally-authenticated user) -- otherwise
   * there is nothing to query server-side and the caller should fall back
   * to filtering whatever conversations are already in memory. */
  isServerHistory: boolean
  /** True once `query` is non-empty AND server search is active -- the
   * caller's cue to render search results instead of the normal list. */
  isSearchMode: boolean
  results: SearchHit[] | null
  isSearching: boolean
  error: string | null
}

/**
 * Owns debounced, cancellation-safe `GET /chat/search` querying, extracted
 * out of the old combined HistoryDrawer so the search box and the
 * conversation list can be independent, independently testable components
 * (see docs/frontend-ui-audit.md's proposed component architecture).
 *
 * "Cancellation-safe" here means stale-response-safe, not a real HTTP abort
 * (no AbortController plumbing exists in src/lib/api.ts yet -- see the
 * audit's "Existing UI Limitations" #2): each request is tagged with an
 * incrementing id, and a response is only applied if it's still the most
 * recent one in flight, so fast typing can't let an older, slower response
 * clobber a newer, faster one.
 */
export function useChatSearch(): ChatSearchState {
  const { t } = useTranslation()
  const isServerHistory = useLocalAuthStore((state) => state.status === 'authenticated')

  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchHit[] | null>(null)
  const [isSearching, setIsSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestId = useRef(0)

  useEffect(() => {
    if (!isServerHistory) return
    const trimmed = query.trim()
    if (!trimmed) {
      setResults(null)
      setIsSearching(false)
      setError(null)
      return
    }
    setIsSearching(true)
    const thisRequestId = ++requestId.current
    const timer = window.setTimeout(() => {
      searchChatHistory(trimmed)
        .then((response) => {
          if (thisRequestId !== requestId.current) return
          setResults(response.results)
          setError(null)
        })
        .catch((caught: unknown) => {
          if (thisRequestId !== requestId.current) return
          setError(caught instanceof ApiError ? caught.message : t('history.searchError'))
          setResults(null)
        })
        .finally(() => {
          if (thisRequestId === requestId.current) setIsSearching(false)
        })
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [query, isServerHistory, t])

  return {
    query,
    setQuery,
    clear: () => setQuery(''),
    isServerHistory,
    isSearchMode: isServerHistory && query.trim().length > 0,
    results,
    isSearching,
    error,
  }
}
