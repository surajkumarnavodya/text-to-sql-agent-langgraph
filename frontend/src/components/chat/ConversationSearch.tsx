import { Loader2, Search, X } from 'lucide-react'
import { forwardRef } from 'react'
import { useTranslation } from 'react-i18next'

export interface ConversationSearchProps {
  value: string
  onChange: (value: string) => void
  isSearching: boolean
}

/** The search box itself -- purely presentational, extracted from the old
 * combined HistoryDrawer so it's independently testable
 * (tests/frontend-ui-audit.md's proposed architecture). Debounce, the
 * server-search call, and result state all live one level up in
 * `useChatSearch`, not here. */
export const ConversationSearch = forwardRef<HTMLInputElement, ConversationSearchProps>(
  function ConversationSearch({ value, onChange, isSearching }, ref) {
    const { t } = useTranslation()
    return (
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--muted-foreground)]"
          aria-hidden="true"
        />
        <input
          ref={ref}
          type="search"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape' && value) {
              event.stopPropagation()
              onChange('')
            }
          }}
          placeholder={t('history.searchPlaceholder')}
          aria-label={t('history.searchPlaceholder')}
          className="h-9 w-full rounded-md border border-[var(--border)] bg-[var(--input)] pl-8 pr-8 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
        />
        {isSearching ? (
          <Loader2
            className="absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-[var(--muted-foreground)]"
            aria-hidden="true"
          />
        ) : (
          value && (
            <button
              type="button"
              onClick={() => onChange('')}
              aria-label={t('history.clearSearch')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )
        )}
      </div>
    )
  },
)
