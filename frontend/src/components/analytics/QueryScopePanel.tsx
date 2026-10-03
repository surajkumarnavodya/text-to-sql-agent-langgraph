import { Expander } from '@/components/ui/expander'
import { sanitizeLabel } from '@/lib/analyticsCharts'
import type { AnalyticalIntent, AnalyticalPlan } from '@/lib/types'

/** Read-only "what was actually asked for" -- the validated analytical plan
 * (Prompt 12) and the intent classification (Prompt 11), rendered as a
 * collapsed expander so the business user can check the scope of the answer
 * (which metric, which breakdown, which filters, which time window) without it
 * crowding the headline. Nothing here can change the executed query. An
 * ambiguity the classifier flagged is shown as a warning, never resolved
 * silently. */
export function QueryScopePanel({
  plan,
  intent,
}: {
  plan: AnalyticalPlan | null
  intent: AnalyticalIntent | null
}) {
  if (!plan && !intent) return null

  const rows: { label: string; value: string }[] = []
  if (plan) {
    if (plan.metrics.length > 0) {
      rows.push({
        label: 'Metrics',
        value: plan.metrics
          .map((m) => `${sanitizeLabel(m.name, 60)}${m.aggregation !== 'none' ? ` (${m.aggregation})` : ''}`)
          .join(', '),
      })
    }
    if (plan.dimensions.length > 0) {
      rows.push({
        label: 'Broken down by',
        value: plan.dimensions.map((d) => sanitizeLabel(d.name, 60)).join(', '),
      })
    }
    if (plan.filters.length > 0) {
      rows.push({
        label: 'Filters',
        value: plan.filters
          .map((f) => sanitizeLabel(`${f.column} ${f.operator} ${f.value}`, 100))
          .join('; '),
      })
    }
    if (plan.time_range) {
      rows.push({ label: 'Time window', value: sanitizeLabel(plan.time_range.description, 120) })
    }
    if (plan.grain) {
      rows.push({ label: 'Grain', value: sanitizeLabel(plan.grain, 40) })
    }
    if (plan.ranking) {
      const top = plan.ranking.top_n !== null ? `top ${plan.ranking.top_n}` : 'ranked'
      rows.push({
        label: 'Ranking',
        value: `${top} by ${sanitizeLabel(plan.ranking.order_by, 60)} (${plan.ranking.direction})`,
      })
    }
    if (plan.limit !== null) {
      rows.push({ label: 'Row limit', value: String(plan.limit) })
    }
  }

  const ambiguities = intent?.ambiguity_flags ?? []

  return (
    <Expander title="Query scope">
      <div className="flex flex-col gap-3 text-sm">
        {intent && (
          <p className="text-xs text-[var(--muted-foreground)]">
            Interpreted as <span className="font-medium">{sanitizeLabel(intent.intent, 40)}</span> (confidence{' '}
            {Math.round(intent.confidence * 100)}%).
          </p>
        )}
        {ambiguities.length > 0 && (
          <ul className="list-disc pl-5 text-xs text-[var(--warning)]">
            {ambiguities.map((flag) => (
              <li key={flag}>{sanitizeLabel(flag, 200)}</li>
            ))}
          </ul>
        )}
        {rows.length > 0 ? (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
            {rows.map((row) => (
              <div key={row.label} className="contents">
                <dt className="text-xs text-[var(--muted-foreground)]">{row.label}</dt>
                <dd className="break-words">{row.value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="text-xs text-[var(--muted-foreground)]">No additional scope was specified.</p>
        )}
      </div>
    </Expander>
  )
}
