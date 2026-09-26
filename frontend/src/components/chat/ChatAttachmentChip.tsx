import { AlertCircle, Crop, Eraser, FileText, Loader2, MoreVertical, Pencil, ScanText, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { ChatAttachment } from '@/hooks/useChatAttachments'
import { formatFileSize } from '@/lib/imageValidation'

/** One attached file's card in the composer -- filename, size, type,
 * upload/processing status, an inline error when applicable, and a remove
 * button, per this feature's own display requirements. An image additionally
 * gets a thumbnail preview + edit button; a document gets a generic file
 * icon instead (there is no meaningful visual preview for a PDF/DOCX/XLSX/
 * PPTX/TXT/CSV/JSON attachment).
 *
 * `onExtractText`/`onResize`/`onRemoveText` are the three explicit image
 * actions (capabilities B/C/D -- see CLAUDE.md's "four capabilities" split)
 * -- deliberately separate, differently-named actions, never folded into
 * "Edit" (which only ever does local crop/rotate/draw, capability-free) or
 * into each other. Only offered once the image has actually finished
 * uploading (a real `attachmentId` exists server-side to act on). */
export function ChatAttachmentChip({
  attachment,
  onEdit,
  onRemove,
  onView,
  onExtractText,
  onResize,
  onRemoveText,
}: {
  attachment: ChatAttachment
  onEdit?: () => void
  onRemove: () => void
  onView?: () => void
  onExtractText?: () => void
  onResize?: () => void
  onRemoveText?: () => void
}) {
  const { t } = useTranslation()
  const previewSrc = attachment.editedDataUrl ?? attachment.previewUrl
  const showImageActionsMenu =
    attachment.kind === 'image' && attachment.status === 'ready' && (onExtractText || onResize || onRemoveText)

  return (
    <div className="flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] p-1.5 pr-2 text-xs">
      {attachment.kind === 'image' && previewSrc ? (
        <button
          type="button"
          onClick={onView}
          aria-label={`${t('image.view')}: ${attachment.file.name}`}
          className="shrink-0 overflow-hidden rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
        >
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- decorative, the wrapping button carries the interaction/label */}
          <img src={previewSrc} alt="" className="h-10 w-10 object-cover" width={40} height={40} />
        </button>
      ) : (
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-[var(--muted)]">
          <FileText className="h-5 w-5 text-[var(--muted-foreground)]" />
        </div>
      )}
      <div className="flex min-w-0 flex-col">
        <span className="max-w-[10rem] truncate font-medium" title={attachment.file.name}>
          {attachment.file.name}
        </span>
        {attachment.status === 'uploading' ? (
          <span className="flex items-center gap-1 text-[var(--muted-foreground)]">
            <Loader2 className="h-3 w-3 animate-spin" /> {t('attachments.uploading')}
          </span>
        ) : attachment.status === 'error' ? (
          <span
            className="flex items-center gap-1 text-[var(--danger)]"
            title={attachment.error ?? undefined}
          >
            <AlertCircle className="h-3 w-3 shrink-0" />
            <span className="truncate">{attachment.error ?? t('attachments.uploadFailed')}</span>
          </span>
        ) : (
          <span className="text-[var(--muted-foreground)]">
            {formatFileSize(attachment.file.size)}
            {attachment.editedDataUrl && ` · ${t('image.edited')}`}
          </span>
        )}
      </div>
      <div className="ml-1 flex items-center gap-0.5">
        {attachment.kind === 'image' && onEdit && (
          <Button
            size="icon"
            variant="ghost"
            onClick={onEdit}
            aria-label={t('image.edit')}
            title={t('image.edit')}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
        )}
        {showImageActionsMenu && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="icon"
                variant="ghost"
                aria-label={t('attachments.moreActions')}
                title={t('attachments.moreActions')}
              >
                <MoreVertical className="h-3.5 w-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              {onExtractText && (
                <DropdownMenuItem onSelect={onExtractText}>
                  <ScanText className="h-3.5 w-3.5" />
                  {t('attachments.actionExtractText')}
                </DropdownMenuItem>
              )}
              {onResize && (
                <DropdownMenuItem onSelect={onResize}>
                  <Crop className="h-3.5 w-3.5" />
                  {t('attachments.actionResize')}
                </DropdownMenuItem>
              )}
              {onRemoveText && (
                <DropdownMenuItem onSelect={onRemoveText}>
                  <Eraser className="h-3.5 w-3.5" />
                  {t('attachments.actionRemoveText')}
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        <Button
          size="icon"
          variant="ghost"
          onClick={onRemove}
          aria-label={t('image.remove')}
          title={t('image.remove')}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  )
}
