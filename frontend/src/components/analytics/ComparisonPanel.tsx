import { formatNumber } from '@/lib/chartEngine'
import { sanitizeLabel } from '@/lib/analyticsCharts'
import type { GrowthStat, RankingStat } from '@/lib/types'
import { AnalyticsPanel } from './AnalyticsPanel'

/** A side-by-side comparison of the two ends of an already-computed series
 * or ranking -- first vs. last period for a time series, top vs. runner-up
 * for a ranking. Shows the engine's own values and percentages as-is; it
 * never derives a new difference or percentage of its own. */
export function ComparisonPanel({
  growth,
  ranking,
  description,
}: {
  growth: GrowthStat | null
  ranking: RankingStat | null
  description: string | null
}) {
  const cleanDescription = description ? sanitizeLabel(description, 200) : null

  if (growth && growth.points.length >= 2) {
    const first = growth.points[0]
    const last = growth.points[growth.points.length - 1]
    const overall = growth.overall_change_percent
    return (
      <AnalyticsPanel id="analytics-comparison" title="Comparison" truthLevel="database_fact">
        {cleanDescription && <p className="text-sm text-[var(--muted-foreground)]">{cleanDescription}</p>}
        <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">{sanitizeLabel(first.period)}</dt>
            <dd className="text-lg font-semibold tabular-nums">{formatNumber(first.value, 'plain')}</dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">{sanitizeLabel(last.period)}</dt>
            <dd className="text-lg font-semibold tabular-nums">{formatNumber(last.value, 'plain')}</dd>
          </div>
          {overall !== null && (
            <div>
              <dt className="text-xs text-[var(--muted-foreground)]">Change</dt>
              <dd className="text-lg font-semibold tabular-nums">
                {overall >= 0 ? '+' : ''}
                {overall.toFixed(1)}%
              </dd>
            </div>
          )}
        </dl>
      </AnalyticsPanel>
    )
  }

  if (ranking && ranking.entries.length >= 2) {
    const [top, runnerUp] = ranking.entries
    return (
      <AnalyticsPanel id="analytics-comparison" title="Comparison" truthLevel="database_fact">
        {cleanDescription && <p className="text-sm text-[var(--muted-foreground)]">{cleanDescription}</p>}
        <dl className="grid grid-cols-2 gap-3 text-sm">
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">{sanitizeLabel(top.label)}</dt>
            <dd className="text-lg font-semibold tabular-nums">
              {formatNumber(top.value, 'plain')}
              {top.share_percent !== null ? ` · ${top.share_percent.toFixed(1)}%` : ''}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-[var(--muted-foreground)]">{sanitizeLabel(runnerUp.label)}</dt>
            <dd className="text-lg font-semibold tabular-nums">
              {formatNumber(runnerUp.value, 'plain')}
              {runnerUp.share_percent !== null ? ` · ${runnerUp.share_percent.toFixed(1)}%` : ''}
            </dd>
          </div>
        </dl>
      </AnalyticsPanel>
    )
  }

  return null
}
