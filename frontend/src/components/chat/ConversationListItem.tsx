import { Check, Loader2, MoreHorizontal, Pencil, Share2, Trash2, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { ConversationSummary } from '@/lib/history'
import { cn } from '@/lib/utils'

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

  return (
    <div
      className={cn(
        'group flex items-center gap-1 rounded px-2 py-1 text-left text-[13px] leading-5 transition-colors',
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
            className="h-6 flex-1 rounded border border-[var(--border)] bg-[var(--input)] px-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
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
            className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
          >
            <span className="min-w-0 flex-1 truncate font-medium">{conversation.title}</span>
            {isActive && isLoading && (
              <>
                <Loader2 className="h-3 w-3 shrink-0 animate-spin" aria-hidden="true" />
                <span className="sr-only">{t('history.loadingConversation')}</span>
              </>
            )}
          </button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="icon"
                variant="ghost"
                className="h-6 w-6 shrink-0 p-0 opacity-0 focus-visible:opacity-100 group-hover:opacity-100"
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
