import { Badge } from '@/components/ui/badge'
import type { RecommendationRecordOut } from '@/lib/types'
import {
  CATEGORY_LABELS,
  formatConfidence,
  formatWhen,
  ownerLabel,
  STATUS_LABELS,
  statusTone,
} from '@/lib/recommendationDisplay'

/** The filtered list of recommendations -- the left column of the Prompt 31
 * dashboard. Each row is labelled as an AI estimate: a recommendation is this
 * platform's suggestion, never a confirmed fact (master rule 10). */
export function RecommendationList({
  records,
  selectedId,
  onSelect,
}: {
  records: RecommendationRecordOut[]
  selectedId: string | null
  onSelect: (recordId: string) => void
}) {
  if (records.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-[var(--border)] p-4 text-sm text-[var(--muted-foreground)]">
        No recommendations match these filters.
      </p>
    )
  }

  return (
    <ul className="flex flex-col gap-2" aria-label="Recommendations">
      {records.map((record) => {
        const selected = record.id === selectedId
        return (
          <li key={record.id}>
            <button
              type="button"
              onClick={() => onSelect(record.id)}
              aria-current={selected ? 'true' : undefined}
              className={`w-full cursor-pointer rounded-lg border p-3 text-left transition-colors ${
                selected
                  ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                  : 'border-[var(--border)] bg-[var(--card)] hover:bg-[var(--muted)]'
              }`}
            >
              <p className="line-clamp-2 text-sm font-medium">{record.claim_text}</p>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <Badge tone={statusTone(record.status)}>{STATUS_LABELS[record.status]}</Badge>
                <Badge tone="warning" data-truth-level="ai_inference">
                  AI estimate
                </Badge>
                {record.category && (
                  <Badge>{CATEGORY_LABELS[record.category as keyof typeof CATEGORY_LABELS] ?? record.category}</Badge>
                )}
              </div>
              <p className="mt-2 text-xs text-[var(--muted-foreground)]">
                {record.database_id} · confidence {formatConfidence(record.confidence)} · {ownerLabel(record)} ·{' '}
                {formatWhen(record.created_at)}
              </p>
            </button>
          </li>
        )
      })}
    </ul>
  )
}
