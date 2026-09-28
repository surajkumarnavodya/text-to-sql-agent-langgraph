import { Check, MoreHorizontal, Pencil, Share2, Trash2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { formatRelativeTime, type ConversationSummary } from '@/lib/history'
import { cn } from '@/lib/utils'

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

export interface ConversationListItemProps {
  conversation: ConversationSummary
  isActive: boolean
  isLoading: boolean
  isRenaming: boolean
  renameValue: string
  onRenameValueChange: (value: string) => void
  onSelect: () => void
  onStartRename: () => void
  onCommitRename: () => void
  onCancelRename: () => void
  onDelete: () => void
  onShare: () => void
}

/** One row in the conversation list -- extracted from the old
 * HistoryDrawer so it can be tested (selection, rename, delete) in
 * isolation from the list's own grouping/loading logic. */
export function ConversationListItem({
  conversation,
  isActive,
  isLoading,
  isRenaming,
  renameValue,
  onRenameValueChange,
  onSelect,
  onStartRename,
  onCommitRename,
  onCancelRename,
  onDelete,
  onShare,
}: ConversationListItemProps) {
  const { t } = useTranslation()
  const lastEntry = conversation.entries.at(-1)

  return (
    <div
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
            onChange={(event) => onRenameValueChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') onCommitRename()
              if (event.key === 'Escape') onCancelRename()
            }}
            aria-label={t('history.rename')}
            className="h-7 flex-1 rounded border border-[var(--border)] bg-[var(--input)] px-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
          />
          <Button size="icon" variant="ghost" onClick={onCommitRename} aria-label={t('common.save')}>
            <Check className="h-3.5 w-3.5" />
          </Button>
          <Button size="icon" variant="ghost" onClick={onCancelRename} aria-label={t('common.cancel')}>
            <X className="h-3.5 w-3.5" />
          </Button>
        </div>
      ) : (
        <>
          <button
            type="button"
            onClick={onSelect}
            aria-current={isActive ? 'true' : undefined}
            className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left"
          >
            <span className="w-full truncate font-medium">{conversation.title}</span>
            <span className="flex items-center gap-1.5 text-xs text-[var(--muted-foreground)]">
              {isActive && isLoading ? t('history.loadingConversation') : formatRelativeTime(conversation.updatedAt)}
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
                aria-label={`${t('history.rename')} / ${t('share.menuItem')} / ${t('history.delete')}`}
              >
                <MoreHorizontal className="h-3.5 w-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuItem onSelect={onStartRename}>
                <Pencil className="h-3.5 w-3.5" />
                {t('history.rename')}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={onShare}>
                <Share2 className="h-3.5 w-3.5" />
                {t('share.menuItem')}
              </DropdownMenuItem>
              <DropdownMenuItem className="text-[var(--danger)]" onSelect={onDelete}>
                <Trash2 className="h-3.5 w-3.5" />
                {t('history.delete')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      )}
    </div>
  )
}
