import { AlertTriangle } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Markdown } from '@/components/ui/markdown'
import type { AttachmentResult } from '@/lib/types'

/** Renders the "attachments" source's contribution -- the answer the model
 * gave from the file(s) the user attached to this question. Distinct from
 * SourceAnswerCard (document/policy/web) since AttachmentResult has no
 * citations list; `used_attachment_ids` (how many attachments actually
 * contributed) and `vision_unavailable` (an attached image existed but no
 * vision model is configured, so it degraded to OCR-only text -- see
 * attachments/graph.py's build_multimodal_message_node) are shown instead. */
export function AttachmentResultCard({ result }: { result: AttachmentResult }) {
  const { t } = useTranslation()
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-sm">
      <p className="mb-1 font-semibold">{t('attachments.sourceLabel')}</p>
      <Markdown>{result.answer}</Markdown>
      {result.vision_unavailable && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-[var(--warning)]">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          {t('attachments.visionUnavailableNotice')}
        </p>
      )}
    </div>
  )
}
