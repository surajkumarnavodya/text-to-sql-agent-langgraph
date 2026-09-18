import { Check, Loader2, MessagesSquare, MoreHorizontal, Pencil, Plus, Search, Trash2, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { ApiError } from '@/lib/api'
import { formatRelativeTime, groupConversationsByRecency, type ConversationGroupKey } from '@/lib/history'
import { searchChatHistory } from '@/lib/identityApi'
import type { SearchHit } from '@/lib/types'
import { cn } from '@/lib/utils'
import { useChatStore } from '@/store/chatStore'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { HistorySettingsSection } from './HistorySettingsSection'

const GROUP_LABEL_KEYS: Record<ConversationGroupKey, string> = {
  today: 'history.today',
  yesterday: 'history.yesterday',
  previous7Days: 'history.previous7Days',
  older: 'history.older',
}

const MOBILE_QUERY = '(max-width: 767px)'
const SEARCH_DEBOUNCE_MS = 300

/** The right-side chat-history panel -- hidden by default, opened and
 * closed by the single header gear button (see AppShell). Never renders
 * its own close control (per the redesign spec, the gear is the only
 * toggle); Escape and the mobile backdrop are the only other ways out.
 *
 * Search behaves differently depending on whether server-side history is
 * active (`useLocalAuthStore`'s `status === 'authenticated'` -- see
 * `chatStore.ts`'s own `isServerHistoryActive`): with it, typing debounces
 * into a real `GET /chat/search` call across the user's *complete*
 * server-side history (not just whatever conversations happen to be
 * loaded already); without it, this falls back to the original plain
 * client-side title filter over the in-memory conversation list, exactly
 * as before this feature existed. */
export function HistoryDrawer({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const searchInputRef = useRef<HTMLInputElement>(null)

  const isServerHistory = useLocalAuthStore((state) => state.status === 'authenticated')
  const [searchResults, setSearchResults] = useState<SearchHit[] | null>(null)
  const [isSearching, setIsSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const searchRequestId = useRef(0)

  const conversations = useChatStore((state) => state.conversations)
  const activeConversationId = useChatStore((state) => state.activeConversationId)
  const isHistoryHydrated = useChatStore((state) => state.isHistoryHydrated)
  const isLoadingConversation = useChatStore((state) => state.isLoadingConversation)
  const startNewChat = useChatStore((state) => state.startNewChat)
  const loadConversation = useChatStore((state) => state.loadConversation)
  const renameConversation = useChatStore((state) => state.renameConversation)
  const deleteConversation = useChatStore((state) => state.deleteConversation)

  useEffect(() => {
    if (!isOpen) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    // Focus the search field on open, matching a standard dialog/drawer's
    // "put the keyboard where the user will most likely want to type
    // first" behavior.
    searchInputRef.current?.focus()
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onClose])

  useEffect(() => {
    if (!isOpen) {
      setSearch('')
      setRenamingId(null)
      setSearchResults(null)
      setSearchError(null)
    }
  }, [isOpen])

  // Debounced server-side search -- see this component's own docstring.
  useEffect(() => {
    if (!isServerHistory) return
    const query = search.trim()
    if (!query) {
      setSearchResults(null)
      setIsSearching(false)
      setSearchError(null)
      return
    }
    setIsSearching(true)
    const requestId = ++searchRequestId.current
    const timer = window.setTimeout(() => {
      searchChatHistory(query)
        .then((response) => {
          if (requestId !== searchRequestId.current) return // a newer keystroke superseded this request
          setSearchResults(response.results)
          setSearchError(null)
        })
        .catch((error: unknown) => {
          if (requestId !== searchRequestId.current) return
          setSearchError(error instanceof ApiError ? error.message : t('history.searchError'))
          setSearchResults(null)
        })
        .finally(() => {
          if (requestId === searchRequestId.current) setIsSearching(false)
        })
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [search, isServerHistory, t])

  const allConversations = useMemo(() => Object.values(conversations), [conversations])
  const locallyFiltered = useMemo(() => {
    if (isServerHistory) return allConversations // server search drives the list instead
    const query = search.trim().toLowerCase()
    if (!query) return allConversations
    return allConversations.filter((conversation) => conversation.title.toLowerCase().includes(query))
  }, [allConversations, search, isServerHistory])
  const groups = useMemo(() => groupConversationsByRecency(locallyFiltered), [locallyFiltered])

  const closeOnMobile = () => {
    if (window.matchMedia(MOBILE_QUERY).matches) onClose()
  }

  const handleSelect = (id: string) => {
    if (renamingId) return
    void loadConversation(id)
    closeOnMobile()
  }

  const handleSelectSearchResult = (hit: SearchHit) => {
    void loadConversation(hit.conversation_id)
    setSearch('')
    setSearchResults(null)
    closeOnMobile()
  }

  const handleNewChat = () => {
    startNewChat()
    closeOnMobile()
  }

  const startRename = (id: string, currentTitle: string) => {
    setRenamingId(id)
    setRenameValue(currentTitle)
  }

  const commitRename = (id: string) => {
    void renameConversation(id, renameValue)
    setRenamingId(null)
  }

  const handleDelete = (id: string, title: string) => {
    if (window.confirm(`Delete "${title}"? This can't be undone.`)) {
      void deleteConversation(id)
    }
  }

  const isSearchMode = isServerHistory && search.trim().length > 0

  return (
    <>
      {/* Backdrop: mobile/tablet only -- on desktop the drawer floats above
          the workspace without blocking interaction with it (see
          AppShell's md:pr-* shift, which keeps the workspace clear of the
          drawer's own width instead of relying on a dimming overlay).
          Starts below the header (top-14, matching AppShell's h-14) so it
          never covers the gear button itself. */}
      {isOpen && (
        <div
          className="fixed inset-x-0 top-14 bottom-0 z-40 bg-black/40 md:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        role="dialog"
        aria-label={t('history.title')}
        aria-hidden={!isOpen}
        className={cn(
          'fixed right-0 top-14 bottom-0 z-40 flex w-full max-w-sm flex-col overflow-hidden',
          'border-l border-[var(--border)] bg-[var(--sidebar)] text-[var(--sidebar-foreground)] shadow-2xl',
          'transition-transform duration-300 ease-in-out motion-reduce:transition-none',
          isOpen ? 'translate-x-0' : 'translate-x-full',
        )}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-[var(--border)] px-4 py-3">
          <h2 className="text-sm font-semibold">{t('history.title')}</h2>
        </div>

        <div className="flex shrink-0 flex-col gap-2 p-3">
          <Button variant="secondary" size="sm" onClick={handleNewChat} className="justify-start">
            <Plus className="h-3.5 w-3.5" />
            {t('history.newChat')}
          </Button>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--muted-foreground)]" />
            <input
              ref={searchInputRef}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Escape' && search) {
                  event.stopPropagation()
                  setSearch('')
                }
              }}
              placeholder={t('history.searchPlaceholder')}
              aria-label={t('history.searchPlaceholder')}
              className="h-9 w-full rounded-md border border-[var(--border)] bg-[var(--input)] pl-8 pr-8 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            />
            {isSearching ? (
              <Loader2 className="absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 animate-spin text-[var(--muted-foreground)]" />
            ) : (
              search && (
                <button
                  type="button"
                  onClick={() => setSearch('')}
                  aria-label={t('history.clearSearch')}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )
            )}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
          {isSearchMode ? (
            <SearchResultsList
              results={searchResults}
              isSearching={isSearching}
              error={searchError}
              onSelect={handleSelectSearchResult}
            />
          ) : (
            <>
              {!isHistoryHydrated && isServerHistory && (
                <div className="flex items-center justify-center py-10">
                  <Loader2 className="h-5 w-5 animate-spin text-[var(--muted-foreground)]" />
                </div>
              )}
              {(!isServerHistory || isHistoryHydrated) && allConversations.length === 0 && (
                <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
                  <MessagesSquare className="h-8 w-8 text-[var(--muted-foreground)]" aria-hidden="true" />
                  <p className="text-sm text-[var(--muted-foreground)]">{t('history.empty')}</p>
                </div>
              )}

              {groups.map((group) => (
                <div key={group.key} className="mb-3">
                  <p className="px-1 py-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted-foreground)]">
                    {t(GROUP_LABEL_KEYS[group.key])}
                  </p>
                  <div className="flex flex-col gap-0.5">
                    {group.items.map((conversation) => {
                      const isActive = conversation.id === activeConversationId
                      const lastEntry = conversation.entries.at(-1)
                      const isRenaming = renamingId === conversation.id
                      return (
                        <div
                          key={conversation.id}
                          className={cn(
                            'group flex items-center gap-1 rounded-md px-2 py-2 text-left text-sm transition-colors',
                            isActive ? 'bg-[var(--accent-soft)] text-[var(--accent)]' : 'hover:bg-[var(--muted)]',
                          )}
                        >
                          {isRenaming ? (
                            <div className="flex flex-1 items-center gap-1">
                              <input
                                autoFocus
                                value={renameValue}
                                onChange={(event) => setRenameValue(event.target.value)}
                                onKeyDown={(event) => {
                                  if (event.key === 'Enter') commitRename(conversation.id)
                                  if (event.key === 'Escape') setRenamingId(null)
                                }}
                                className="h-7 flex-1 rounded border border-[var(--border)] bg-[var(--input)] px-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                              />
                              <Button size="icon" variant="ghost" onClick={() => commitRename(conversation.id)}>
                                <Check className="h-3.5 w-3.5" />
                              </Button>
                              <Button size="icon" variant="ghost" onClick={() => setRenamingId(null)}>
                                <X className="h-3.5 w-3.5" />
                              </Button>
                            </div>
                          ) : (
                            <>
                              <button
                                type="button"
                                onClick={() => handleSelect(conversation.id)}
                                className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left"
                              >
                                <span className="w-full truncate font-medium">{conversation.title}</span>
                                <span className="flex items-center gap-1.5 text-xs text-[var(--muted-foreground)]">
                                  {isActive && isLoadingConversation
                                    ? t('history.loadingConversation')
                                    : formatRelativeTime(conversation.updatedAt)}
                                  {lastEntry && (
                                    <>
                                      <span aria-hidden="true">·</span>
                                      <StatusDot status={lastEntry.agentStatus} />
                                    </>
                                  )}
                                </span>
                              </button>
                              <DropdownMenu>
                                <DropdownMenuTrigger asChild>
                                  <Button
                                    size="icon"
                                    variant="ghost"
                                    className="shrink-0 opacity-0 focus-visible:opacity-100 group-hover:opacity-100"
                                    aria-label={`${t('history.rename')} / ${t('history.delete')}`}
                                  >
                                    <MoreHorizontal className="h-3.5 w-3.5" />
                                  </Button>
                                </DropdownMenuTrigger>
                                <DropdownMenuContent>
                                  <DropdownMenuItem onSelect={() => startRename(conversation.id, conversation.title)}>
                                    <Pencil className="h-3.5 w-3.5" />
                                    {t('history.rename')}
                                  </DropdownMenuItem>
                                  <DropdownMenuItem
                                    className="text-[var(--danger)]"
                                    onSelect={() => handleDelete(conversation.id, conversation.title)}
                                  >
                                    <Trash2 className="h-3.5 w-3.5" />
                                    {t('history.delete')}
                                  </DropdownMenuItem>
                                </DropdownMenuContent>
                              </DropdownMenu>
                            </>
                          )}
                        </div>
                      )
                    })}
                  </div>
                </div>
              ))}
            </>
          )}

          <HistorySettingsSection />
        </div>
      </aside>
    </>
  )
}

function SearchResultsList({
  results,
  isSearching,
  error,
  onSelect,
}: {
  results: SearchHit[] | null
  isSearching: boolean
  error: string | null
  onSelect: (hit: SearchHit) => void
}) {
  const { t } = useTranslation()

  if (error) {
    return <p className="px-1 py-6 text-center text-sm text-[var(--danger)]">{error}</p>
  }
  if (results === null && isSearching) {
    return (
      <div className="flex items-center justify-center py-10">
        <Loader2 className="h-5 w-5 animate-spin text-[var(--muted-foreground)]" />
      </div>
    )
  }
  if (results !== null && results.length === 0) {
    return <p className="px-1 py-6 text-center text-sm text-[var(--muted-foreground)]">{t('history.noResults')}</p>
  }
  return (
    <div className="flex flex-col gap-0.5">
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

const STATUS_TONE: Record<string, string> = {
  succeeded: 'bg-[var(--success)]',
  failed: 'bg-[var(--danger)]',
  rejected: 'bg-[var(--danger)]',
  rate_limited: 'bg-[var(--warning)]',
  needs_clarification: 'bg-[var(--warning)]',
}

function StatusDot({ status }: { status: string }) {
  return (
    <span
      className={cn('inline-block h-1.5 w-1.5 rounded-full', STATUS_TONE[status] ?? 'bg-[var(--muted-foreground)]')}
      aria-hidden="true"
    />
  )
}
