import { Loader2, MessagesSquare } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { groupConversationsByRecency, type ConversationGroupKey, type ConversationSummary } from '@/lib/history'
import { ConversationListItem } from './ConversationListItem'

const GROUP_LABEL_KEYS: Record<ConversationGroupKey, string> = {
  today: 'history.today',
  yesterday: 'history.yesterday',
  previous7Days: 'history.previous7Days',
  older: 'history.older',
}

export interface ConversationListProps {
  conversations: ConversationSummary[]
  activeConversationId: string | null
  isLoadingConversation: boolean
  isLoading: boolean
  renamingId: string | null
  renameValue: string
  onRenameValueChange: (value: string) => void
  onSelect: (id: string) => void
  onStartRename: (id: string, currentTitle: string) => void
  onCommitRename: (id: string) => void
  onCancelRename: () => void
  onDelete: (id: string, title: string) => void
  onShare: (id: string, title: string) => void
}

/** Recency-grouped conversation list -- extracted from the old
 * HistoryDrawer. Owns only rendering/grouping; every mutation (select,
 * rename, delete) is a callback into the caller, which is what makes this
 * reusable from both the desktop Sidebar and the mobile drawer without
 * duplicating any chatStore wiring. */
export function ConversationList({
  conversations,
  activeConversationId,
  isLoadingConversation,
  isLoading,
  renamingId,
  renameValue,
  onRenameValueChange,
  onSelect,
  onStartRename,
  onCommitRename,
  onCancelRename,
  onDelete,
  onShare,
}: ConversationListProps) {
  const { t } = useTranslation()
  const groups = groupConversationsByRecency(conversations)

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-10">
        <Loader2 className="h-5 w-5 animate-spin text-[var(--muted-foreground)]" aria-hidden="true" />
      </div>
    )
  }

  if (conversations.length === 0) {
    return (
      <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
        <MessagesSquare className="h-8 w-8 text-[var(--muted-foreground)]" aria-hidden="true" />
        <p className="text-sm text-[var(--muted-foreground)]">{t('history.empty')}</p>
      </div>
    )
  }

  return (
    <>
      {groups.map((group) => (
        <div key={group.key} className="mb-2">
          <p className="px-1 py-1 text-[11px] font-semibold uppercase tracking-wide text-[var(--muted-foreground)]">
            {t(GROUP_LABEL_KEYS[group.key])}
          </p>
          <div className="flex flex-col gap-px">
            {group.items.map((conversation) => (
              <ConversationListItem
                key={conversation.id}
                conversation={conversation}
                isActive={conversation.id === activeConversationId}
                isLoading={isLoadingConversation}
                isRenaming={renamingId === conversation.id}
                renameValue={renameValue}
                onRenameValueChange={onRenameValueChange}
                onSelect={() => onSelect(conversation.id)}
                onStartRename={() => onStartRename(conversation.id, conversation.title)}
                onCommitRename={() => onCommitRename(conversation.id)}
                onCancelRename={onCancelRename}
                onDelete={() => onDelete(conversation.id, conversation.title)}
                onShare={() => onShare(conversation.id, conversation.title)}
              />
            ))}
          </div>
        </div>
      ))}
    </>
  )
}
