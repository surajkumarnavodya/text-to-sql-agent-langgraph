import { ResultChart } from '@/components/sql/ResultChart'
import { distributionToPrepared, drillDownQuestion, rankingToPrepared, sanitizeLabel } from '@/lib/analyticsCharts'
import { formatNumber } from '@/lib/chartEngine'
import type { RankingStat } from '@/lib/types'
import { AnalyticsPanel } from './AnalyticsPanel'

const MAX_LISTED_ENTRIES = 10

/** A ranked breakdown (`RankingStat`), shown either as horizontal bars
 * (`variant="bars"`, the default for a categorical comparison) or as a
 * share-of-total doughnut (`variant="share"`, for a distribution question).
 * Both variants read the same stat -- a distribution is the same ranking
 * viewed by its share percentages, not a second computation.
 *
 * Each listed row is a drill-down: activating it submits a fixed-template
 * follow-up question about that category through `onDrillDown`. Without that
 * callback the rows are plain text (used by tests and any read-only caller). */
export function RankingPanel({
  ranking,
  variant,
  color,
  onDrillDown,
}: {
  ranking: RankingStat
  variant: 'bars' | 'share'
  color: string
  onDrillDown?: (question: string) => void
}) {
  const listed = ranking.entries.slice(0, MAX_LISTED_ENTRIES)
  const title =
    variant === 'share'
      ? `Distribution of ${sanitizeLabel(ranking.value_column)} by ${sanitizeLabel(ranking.label_column)}`
      : `${sanitizeLabel(ranking.value_column)} by ${sanitizeLabel(ranking.label_column)}`

  const prepared =
    variant === 'share' ? distributionToPrepared(ranking, color) : rankingToPrepared(ranking, color)

  return (
    <AnalyticsPanel id={`analytics-ranking-${variant}`} title={title} truthLevel="database_fact">
      <ResultChart
        prepared={prepared}
        chartType={variant === 'share' ? 'doughnut' : 'bar-horizontal'}
        title=""
        showLegend={variant === 'share'}
        numberFormat="plain"
        stacked={false}
      />
      <ol className="flex flex-col gap-1 text-sm">
        {listed.map((entry) => {
          const label = sanitizeLabel(entry.label)
          const value = formatNumber(entry.value, 'plain')
          const share = entry.share_percent !== null ? ` · ${entry.share_percent.toFixed(1)}%` : ''
          return (
            <li key={`${entry.rank}-${entry.label}`} className="flex items-center justify-between gap-3">
              {onDrillDown ? (
                <button
                  type="button"
                  className="truncate text-left font-medium text-[var(--accent)] hover:underline"
                  onClick={() => onDrillDown(drillDownQuestion(entry.label, ranking.value_column))}
                >
                  {entry.rank}. {label}
                </button>
              ) : (
                <span className="truncate">
                  {entry.rank}. {label}
                </span>
              )}
              <span className="shrink-0 tabular-nums text-[var(--muted-foreground)]">
                {value}
                {share}
              </span>
            </li>
          )
        })}
      </ol>
      {ranking.entries.length > listed.length && (
        <p className="text-xs text-[var(--muted-foreground)]">
          Showing {listed.length} of {ranking.entries.length} values.
        </p>
      )}
      {ranking.truncated && (
        <p className="text-xs text-[var(--warning)]">
          The result was capped by the row limit, so this ranking may be missing categories.
        </p>
      )}
    </AnalyticsPanel>
  )
}
