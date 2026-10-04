import { Plus } from 'lucide-react'
import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { ConversationList } from '@/components/chat/ConversationList'
import { ConversationSearch } from '@/components/chat/ConversationSearch'
import { ConversationSearchResults } from '@/components/chat/ConversationSearchResults'
import { ShareModal } from '@/components/chat/ShareModal'
import { Button } from '@/components/ui/button'
import { useChatSearch } from '@/hooks/useChatSearch'
import type { SearchHit } from '@/lib/types'
import { useChatStore } from '@/store/chatStore'

export interface SidebarProps {
  /** Called after New Chat / selecting a conversation or search result --
   * the mobile drawer uses this to close itself; the persistent desktop
   * sidebar passes nothing, since there's nothing to close. */
  onNavigate?: () => void
  /** Desktop icon-rail mode -- omit (or pass `false`) for the always-fully-
   * expanded form MobileNav's off-canvas drawer uses. Reads from the same
   * single `settingsStore.sidebarCollapsed` source of truth as the header's
   * own `SidebarToggle`; MobileNav deliberately never passes this, since
   * collapsing to icons inside a full-height mobile overlay (which is
   * already an explicit "I want to see my history" action) doesn't make
   * sense -- it always renders the expanded content regardless of the
   * desktop collapse preference. */
  collapsed?: boolean
}

/** Left conversation-history rail -- the history half of what used to be
 * a single combined right-side drawer (HistoryDrawer, now retired). Used
 * both as the persistent desktop sidebar (AppShell) and, unmodified,
 * as the content of the mobile navigation drawer (MobileNav), so there is
 * exactly one implementation of "list + search + new chat" instead of two
 * to keep in sync.
 *
 * Account-level actions (settings, theme, sign out) do NOT live here --
 * see `UserMenu` (rendered once, in the header) and
 * docs/navigation-and-actions.md. This component owns exactly one concern:
 * conversation history navigation. */
export function Sidebar({ onNavigate, collapsed = false }: SidebarProps) {
  const { t } = useTranslation()
  const search = useChatSearch()
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [sharingConversation, setSharingConversation] = useState<{ id: string; title: string } | null>(
    null,
  )
  const searchInputRef = useRef<HTMLInputElement>(null)

  const conversations = useChatStore((state) => state.conversations)
  const activeConversationId = useChatStore((state) => state.activeConversationId)
  const isHistoryHydrated = useChatStore((state) => state.isHistoryHydrated)
  const isLoadingConversation = useChatStore((state) => state.isLoadingConversation)
  const startNewChat = useChatStore((state) => state.startNewChat)
  const loadConversation = useChatStore((state) => state.loadConversation)
  const renameConversation = useChatStore((state) => state.renameConversation)
  const deleteConversation = useChatStore((state) => state.deleteConversation)

  const allConversations = Object.values(conversations)
  const displayedConversations = search.isServerHistory
    ? allConversations // server search drives the results list instead
    : allConversations.filter((conversation) =>
        conversation.title.toLowerCase().includes(search.query.trim().toLowerCase()),
      )

  // The conversation list only exists on the chat screen, so starting or
  // opening a chat from any other AI Workspace page must also take the user
  // back to the chat route -- otherwise the chat state changes out of sight.
  const navigate = useNavigate()

  const handleNewChat = () => {
    startNewChat()
    navigate('/')
    onNavigate?.()
  }

  const handleSelect = (id: string) => {
    if (renamingId) return
    void loadConversation(id)
    navigate('/')
    onNavigate?.()
  }

  const handleSelectSearchResult = (hit: SearchHit) => {
    void loadConversation(hit.conversation_id)
    search.clear()
    navigate('/')
    onNavigate?.()
  }

  const handleDelete = (id: string, title: string) => {
    if (window.confirm(t('history.deleteConfirm', { title }))) {
      void deleteConversation(id)
    }
  }

  if (collapsed) {
    // Icon rail: New Chat only -- expand/collapse itself is owned solely by
    // the header's SidebarToggle (see docs/navigation-and-actions.md; kept
    // to one stable, always-in-the-same-place control rather than also
    // duplicating a second toggle in here). A conversation list of bare
    // titles can't render meaningfully at icon-only width either, so it
    // (and search) are hidden rather than squeezed into illegibility --
    // expanding restores everything exactly where it was; nothing about
    // the underlying history state changes while collapsed.
    return (
      <div className="flex h-full flex-col items-center gap-2 py-3">
        <Button
          variant="ghost"
          size="icon"
          onClick={handleNewChat}
          aria-label={t('history.newChat')}
          title={t('history.newChat')}
        >
          <Plus className="h-4 w-4" />
        </Button>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 flex-col gap-2 p-3">
        <Button variant="secondary" size="sm" onClick={handleNewChat} className="justify-start">
          <Plus className="h-3.5 w-3.5" />
          {t('history.newChat')}
        </Button>
        <ConversationSearch
          ref={searchInputRef}
          value={search.query}
          onChange={search.setQuery}
          isSearching={search.isSearching}
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {search.isSearchMode ? (
          <ConversationSearchResults
            results={search.results}
            isSearching={search.isSearching}
            error={search.error}
            onSelect={handleSelectSearchResult}
          />
        ) : (
          <ConversationList
            conversations={displayedConversations}
            activeConversationId={activeConversationId}
            isLoadingConversation={isLoadingConversation}
            isLoading={search.isServerHistory && !isHistoryHydrated}
            renamingId={renamingId}
            renameValue={renameValue}
            onRenameValueChange={setRenameValue}
            onSelect={handleSelect}
            onStartRename={(id, title) => {
              setRenamingId(id)
              setRenameValue(title)
            }}
            onCommitRename={(id) => {
              void renameConversation(id, renameValue)
              setRenamingId(null)
            }}
            onCancelRename={() => setRenamingId(null)}
            onDelete={handleDelete}
            onShare={(id, title) => setSharingConversation({ id, title })}
          />
        )}
      </div>
      {sharingConversation && (
        <ShareModal
          conversationId={sharingConversation.id}
          conversationTitle={sharingConversation.title}
          onClose={() => setSharingConversation(null)}
        />
      )}
    </div>
  )
}
