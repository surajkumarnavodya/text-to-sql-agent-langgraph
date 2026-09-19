import { Download } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import { Markdown } from '@/components/ui/markdown'
import { downloadDocument } from '@/lib/api'
import type { Citation, SourceAnswer } from '@/lib/types'

const SOURCE_LABELS: Record<string, string> = {
  document_result: 'Documents',
  policy_result: 'Policy',
  web_result: 'Web',
}

function isUrl(value: string): boolean {
  return /^https?:\/\//i.test(value)
}

/** One entry in the "Sources: ..." line -- a real clickable link, not
 * plain text. A web citation's `filename` is the page URL itself (see
 * agent/orchestrator/nodes.py::web_search_node), so it opens directly; a
 * document/policy citation with stored PDF bytes triggers the same
 * download as the button row below instead, since there's no external URL
 * to open. A citation with neither (an older upload, ingested before
 * ENABLE_PDF_DOWNLOAD existed) stays plain text -- there's genuinely
 * nothing to link to.
 *
 * Page number is shown when the backend sent one (`Citation.page_number`,
 * real data that existed but wasn't previously surfaced here) -- a
 * per-message date/excerpt is NOT shown, because `Citation` genuinely has
 * no such fields today (confirmed against `api/schemas.py`/
 * `agent/orchestrator/nodes.py`'s actual citation construction); adding
 * either would mean fabricating data, not surfacing it. See
 * docs/frontend-ui-audit.md for this as a named, backend-dependent gap. */
function SourceLink({ citation }: { citation: Citation }) {
  const pageSuffix = citation.page_number != null ? ` (p. ${citation.page_number})` : ''
  if (isUrl(citation.filename)) {
    return (
      <a
        href={citation.filename}
        target="_blank"
        rel="noopener noreferrer"
        className="text-[var(--accent)] underline underline-offset-2 hover:opacity-80"
      >
        {citation.filename}
      </a>
    )
  }
  if (citation.has_pdf_bytes) {
    return (
      <button
        type="button"
        onClick={() => void downloadDocument(citation.document_id, citation.filename)}
        className="text-[var(--accent)] underline underline-offset-2 hover:opacity-80"
      >
        {citation.filename}
        {pageSuffix}
      </button>
    )
  }
  return (
    <span>
      {citation.filename}
      {pageSuffix}
    </span>
  )
}

export function SourceAnswerCard({ sourceKey, result }: { sourceKey: string; result: SourceAnswer }) {
  const { t } = useTranslation()
  const seen = new Set<string>()
  const uniqueCitations = result.citations.filter((citation) => {
    const key = citation.document_id || citation.filename
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
  const downloadable = uniqueCitations.filter((citation) => citation.has_pdf_bytes && !isUrl(citation.filename))

  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-3 text-sm">
      <p className="mb-1 font-semibold">{SOURCE_LABELS[sourceKey] ?? sourceKey}</p>
      <Markdown>{result.answer}</Markdown>
      {uniqueCitations.length > 0 && (
        <div className="mt-2 text-xs text-[var(--muted-foreground)]">
          <p>{t('chat.sourcesLabel')}:</p>
          <ul className="mt-1 flex flex-col gap-1">
            {uniqueCitations.map((citation) => (
              <li key={citation.document_id || citation.filename}>
                <SourceLink citation={citation} />
              </li>
            ))}
          </ul>
        </div>
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
