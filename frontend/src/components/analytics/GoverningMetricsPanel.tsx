import { sanitizeLabel } from '@/lib/analyticsCharts'
import type { GoverningMetric } from '@/lib/types'
import { AnalyticsPanel } from './AnalyticsPanel'

/** The approved metric definitions this answer was grounded in (Prompt 10) --
 * each one is a published, human-reviewed `CONFIRMED_BUSINESS_TRUTH` entry from
 * the semantic catalog, so its definition is shown verbatim as the reference
 * the business user can check the answer against. Renders nothing when no
 * governed metric matched (the common case). */
export function GoverningMetricsPanel({ metrics }: { metrics: GoverningMetric[] }) {
  if (metrics.length === 0) return null

  return (
    <AnalyticsPanel id="analytics-governing-metrics" title="Approved definitions used" truthLevel="confirmed_business_truth">
      <ul className="flex flex-col gap-3 text-sm">
        {metrics.map((metric, index) => (
          <li key={`${index}-${metric.business_name}`} className="flex flex-col gap-1">
            <p className="font-medium">{sanitizeLabel(metric.business_name, 120)}</p>
            {metric.approved_expression && (
              <code className="block break-all rounded bg-[var(--muted)] px-2 py-1 text-xs">
                {sanitizeLabel(metric.approved_expression, 300)}
              </code>
            )}
            {metric.text && (
              <p className="text-xs text-[var(--muted-foreground)]">{sanitizeLabel(metric.text, 400)}</p>
            )}
          </li>
        ))}
      </ul>
    </AnalyticsPanel>
  )
}
