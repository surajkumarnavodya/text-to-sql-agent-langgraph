import { ResultChart } from '@/components/sql/ResultChart'
import { growthToPrepared, sanitizeLabel } from '@/lib/analyticsCharts'
import type { GrowthStat } from '@/lib/types'
import { AnalyticsPanel } from './AnalyticsPanel'

const MAX_LISTED_MISSING_PERIODS = 10

/** A period-by-period trend (`GrowthStat`) -- one line chart plus the
 * backend's own overall change and any gaps in the period sequence. The
 * percentage and direction are the engine's own already-computed values,
 * shown as-is, never recomputed here. */
export function TrendPanel({ growth, color }: { growth: GrowthStat; color: string }) {
  const prepared = growthToPrepared(growth, color)
  const overall = growth.overall_change_percent
  const directionWord = growth.direction === 'up' ? 'rose' : growth.direction === 'down' ? 'fell' : 'stayed flat'
  const overallText = overall !== null ? ` (${overall >= 0 ? '+' : ''}${overall.toFixed(1)}% overall)` : ''
  const summary =
    growth.direction !== null
      ? `Over ${growth.points.length} periods it ${directionWord}${overallText}.`
      : `${growth.points.length} periods${overallText}.`
  const missing = growth.missing_periods.slice(0, MAX_LISTED_MISSING_PERIODS).map((p) => sanitizeLabel(p))
  const hiddenMissing = growth.missing_periods.length - missing.length

  return (
    <AnalyticsPanel id="analytics-trend" title={`Trend of ${sanitizeLabel(growth.value_column)}`} truthLevel="database_fact">
      <ResultChart
        prepared={prepared}
        chartType="line"
        title=""
        showLegend={false}
        numberFormat="plain"
        stacked={false}
      />
      <p className="text-sm text-[var(--muted-foreground)]">{summary}</p>
      {missing.length > 0 && (
        <p className="text-xs text-[var(--warning)]">
          Missing periods in the series: {missing.join(', ')}
          {hiddenMissing > 0 ? ` and ${hiddenMissing} more` : ''}.
        </p>
      )}
    </AnalyticsPanel>
  )
}
