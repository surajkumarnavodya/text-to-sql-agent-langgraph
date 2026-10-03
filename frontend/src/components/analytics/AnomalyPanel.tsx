import { sanitizeLabel } from '@/lib/analyticsCharts'
import { formatNumber } from '@/lib/chartEngine'
import type { AnomalyPoint, AnomalySignal } from '@/lib/types'
import { AnalyticsPanel } from './AnalyticsPanel'

const METHOD_LABELS: Record<AnomalySignal['method'], string> = {
  threshold: 'Absolute threshold',
  percent_change: 'Period-over-period change',
  rolling_zscore: 'Rolling z-score',
  iqr: 'Interquartile range',
  seasonal: 'Same period last cycle',
}

/** Flagged points from the deterministic anomaly detector (Prompt 14). A
 * point can be flagged by several methods; each method's own evidence is
 * listed with its formula so the flag is inspectable, never a bare "unusual"
 * label. Renders nothing when there's nothing flagged -- the caller decides
 * whether a series was checked at all (see AnalyticsSummary). */
export function AnomalyPanel({ anomalies }: { anomalies: AnomalyPoint[] }) {
  if (anomalies.length === 0) {
    return (
      <AnalyticsPanel id="analytics-anomalies" title="Anomalies" truthLevel="database_fact">
        <p className="text-sm text-[var(--muted-foreground)]">
          No unusual points were flagged in this series.
        </p>
      </AnalyticsPanel>
    )
  }

  return (
    <AnalyticsPanel id="analytics-anomalies" title={`Anomalies (${anomalies.length} flagged)`} truthLevel="database_fact">
      <ul className="flex flex-col gap-3">
        {anomalies.map((point) => (
          <li key={point.period} className="rounded-md border border-[var(--warning)]/40 p-3 text-sm">
            <p className="font-medium">
              {sanitizeLabel(point.period)}: {formatNumber(point.value, 'plain')}
            </p>
            <ul className="mt-1 flex flex-col gap-1 text-xs text-[var(--muted-foreground)]">
              {point.signals.map((signal) => (
                <li key={`${signal.method}-${signal.formula}`}>
                  {METHOD_LABELS[signal.method]}: deviation {formatNumber(signal.deviation, 'plain')} against a
                  threshold of {formatNumber(signal.threshold_used, 'plain')}. <span className="font-mono">{sanitizeLabel(signal.formula, 120)}</span>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </AnalyticsPanel>
  )
}
