import { Pencil, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import type { AttachedImage } from '@/hooks/useImageAttachments'
import { formatFileSize } from '@/lib/imageValidation'

export function AttachmentChip({
  image,
  onEdit,
  onRemove,
  onView,
}: {
  image: AttachedImage
  onEdit: () => void
  onRemove: () => void
  onView: () => void
}) {
  const { t } = useTranslation()
  const previewSrc = image.editedDataUrl ?? image.originalUrl

  return (
    <div className="flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--card)] p-1.5 pr-2 text-xs">
      <button
        type="button"
        onClick={onView}
        aria-label={`${t('image.view')}: ${image.file.name}`}
        className="shrink-0 overflow-hidden rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]"
      >
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- decorative, the wrapping button carries the interaction/label */}
        <img src={previewSrc} alt="" className="h-10 w-10 object-cover" width={40} height={40} />
      </button>
      <div className="flex min-w-0 flex-col">
        <span className="max-w-[10rem] truncate font-medium" title={image.file.name}>
          {image.file.name}
        </span>
        <span className="text-[var(--muted-foreground)]">
          {formatFileSize(image.file.size)}
          {image.editedDataUrl && ` · ${t('image.edited')}`}
        </span>
      </div>
      <div className="ml-1 flex items-center gap-0.5">
        <Button size="icon" variant="ghost" onClick={onEdit} aria-label={t('image.edit')} title={t('image.edit')}>
          <Pencil className="h-3.5 w-3.5" />
        </Button>
        <Button size="icon" variant="ghost" onClick={onRemove} aria-label={t('image.remove')} title={t('image.remove')}>
          <X className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  )
}
