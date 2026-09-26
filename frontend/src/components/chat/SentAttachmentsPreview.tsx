import { FileText } from 'lucide-react'
import type { SentAttachmentPreview } from '@/lib/history'

/** Read-only thumbnail row shown above a sent question's own chat bubble --
 * see `SentAttachmentPreview`'s own docstring for why this exists: without
 * it, a question sent with an attachment had no visible record of that
 * attachment anywhere in the transcript itself (only the composer showed
 * it, and the composer doesn't visually read as "already sent"). Renders
 * nothing for the overwhelming majority of turns (no attachment). */
export function SentAttachmentsPreview({
  attachments,
}: {
  attachments: SentAttachmentPreview[]
}) {
  if (attachments.length === 0) return null

  return (
    <div className="flex flex-wrap justify-end gap-1.5">
      {attachments.map((attachment, index) =>
        attachment.kind === 'image' && attachment.previewUrl ? (
          <img
            key={index}
            src={attachment.previewUrl}
            alt={attachment.filename}
            title={attachment.filename}
            className="h-14 w-14 shrink-0 rounded-lg border border-[var(--border)] object-cover"
            width={56}
            height={56}
          />
        ) : (
          <span
            key={index}
            title={attachment.filename}
            className="flex max-w-[10rem] items-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--card)] px-2 py-1.5 text-xs"
          >
            <FileText className="h-3.5 w-3.5 shrink-0 text-[var(--muted-foreground)]" />
            <span className="truncate">{attachment.filename}</span>
          </span>
        ),
      )}
    </div>
  )
}
