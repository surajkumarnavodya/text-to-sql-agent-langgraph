import { Download } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Markdown } from '@/components/ui/markdown'
import { downloadDocument } from '@/lib/api'
import type { SourceAnswer } from '@/lib/types'

const SOURCE_LABELS: Record<string, string> = {
  document_result: 'Documents',
  policy_result: 'Policy',
  web_result: 'Web',
}

export function SourceAnswerCard({ sourceKey, result }: { sourceKey: string; result: SourceAnswer }) {
  const { t } = useTranslation()
  const seen = new Set<string>()
  const downloadable = result.citations.filter((citation) => {
    if (!citation.has_pdf_bytes || seen.has(citation.document_id)) return false
    seen.add(citation.document_id)
    return true
  })
  const uniqueFilenames = [...new Set(result.citations.map((c) => c.filename))]

  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-sm">
      <p className="mb-1 font-semibold">{SOURCE_LABELS[sourceKey] ?? sourceKey}</p>
      <Markdown>{result.answer}</Markdown>
      {uniqueFilenames.length > 0 && (
        <p className="mt-2 text-xs text-[var(--muted-foreground)]">Sources: {uniqueFilenames.join(', ')}</p>
      )}
      {downloadable.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {downloadable.map((citation) => (
            <Button
              key={citation.document_id}
              size="sm"
              variant="secondary"
              onClick={() => void downloadDocument(citation.document_id, citation.filename)}
            >
              <Download className="h-3.5 w-3.5" />
              {t('common.download')} {citation.filename}
            </Button>
          ))}
        </div>
      )}
    </div>
  )
}
