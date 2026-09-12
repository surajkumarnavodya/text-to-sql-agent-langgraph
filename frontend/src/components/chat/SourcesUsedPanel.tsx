import { useTranslation } from 'react-i18next'
import { Badge } from '@/components/ui/badge'
import { Markdown } from '@/components/ui/markdown'
import type { AskResponse } from '@/lib/types'
import { MediaResultCard } from './MediaResultCard'
import { SourceAnswerCard } from './SourceAnswerCard'

const SOURCE_CHIP_LABELS: Record<string, string> = {
  sql: 'Database',
  documents: 'Documents',
  policy: 'Policy',
  web: 'Web',
  generation: 'Generated Media',
}

/** Mirrors ui/app.py's _render_sources_used: shows which source(s)
 * contributed, and either the synthesized combined answer or each
 * non-SQL source's own card -- never both, and never a "not found in X"
 * aside once another source already answered (see agent/orchestrator/
 * nodes.py::synthesis_node, which already filters that out server-side).
 *
 * `generation_result` is the one exception to "never both": the actual
 * generated image/video always renders as its own component *in addition
 * to* the synthesized text, regardless of whether the router also picked
 * another source alongside "generation" (it does, often enough that this
 * must be handled, not just the common single-source case -- see
 * synthesis_node's docstring). Rendering it inside the ternary would mean
 * a real user-reported bug: ask to "generate an image of X", the router
 * also picks "web", and the image silently never appears because only
 * the combined text branch rendered. */
export function SourcesUsedPanel({ state }: { state: AskResponse }) {
  const { t } = useTranslation()
  if (state.sources_used.length === 0) return null

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-[var(--muted-foreground)]">
        <span>{t('chat.sources')}:</span>
        {state.sources_used.map((source) => (
          <Badge key={source} tone="accent">
            {SOURCE_CHIP_LABELS[source] ?? source}
          </Badge>
        ))}
      </div>
      {state.synthesized_answer ? (
        <Markdown>{state.synthesized_answer}</Markdown>
      ) : (
        <>
          {state.document_result && <SourceAnswerCard sourceKey="document_result" result={state.document_result} />}
          {state.policy_result && <SourceAnswerCard sourceKey="policy_result" result={state.policy_result} />}
          {state.web_result && <SourceAnswerCard sourceKey="web_result" result={state.web_result} />}
        </>
      )}
      {state.generation_result && <MediaResultCard result={state.generation_result} />}
    </div>
  )
}
