import { Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { SearchHit } from '@/lib/types'

export interface ConversationSearchResultsProps {
  results: SearchHit[] | null
  isSearching: boolean
  error: string | null
  onSelect: (hit: SearchHit) => void
}

/** Renders `GET /chat/search` results -- title, matching excerpt, and
 * (implicitly, via selection) the conversation/message id needed to jump
 * to it. Extracted from the old HistoryDrawer as its own component per
 * docs/frontend-ui-audit.md's proposed architecture. */
export function ConversationSearchResults({
  results,
  isSearching,
  error,
  onSelect,
}: ConversationSearchResultsProps) {
  const { t } = useTranslation()

  if (error) {
    return (
      <p role="alert" className="px-1 py-6 text-center text-sm text-[var(--danger)]">
        {error}
      </p>
    )
  }
  if (results === null && isSearching) {
    return (
      <div className="flex items-center justify-center py-10">
        <Loader2 className="h-5 w-5 animate-spin text-[var(--muted-foreground)]" aria-hidden="true" />
      </div>
    )
  }
  if (results !== null && results.length === 0) {
    return (
      <p className="px-1 py-6 text-center text-sm text-[var(--muted-foreground)]" role="status">
        {t('history.noResults')}
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-0.5" role="status" aria-label={t('history.resultCount', { count: results?.length ?? 0 })}>
      {(results ?? []).map((hit) => (
        <button
          key={`${hit.conversation_id}-${hit.message_id ?? 'title'}`}
          type="button"
          onClick={() => onSelect(hit)}
          className="flex flex-col items-start gap-0.5 rounded-md px-2 py-2 text-left text-sm hover:bg-[var(--muted)]"
        >
          <span className="w-full truncate font-medium">{hit.title ?? t('history.untitled')}</span>
          <span className="w-full truncate text-xs text-[var(--muted-foreground)]">{hit.snippet}</span>
        </button>
      ))}
    </div>
  )
}
