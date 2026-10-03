import { formatNumber, type NumberFormat } from '@/lib/chartEngine'

/** A single-value stat card -- the "one numeric aggregate" chart type
 * (never a one-bar bar chart, per this feature's own chart-type rules).
 * Deliberately not a Chart.js canvas -- a hero number reads clearer as
 * plain typography than as a "chart" with one data point.
 *
 * `delta` (Prompt 30) is an optional percent change versus the previous
 * period, shown as a signed badge. Omitted or `null` renders exactly the card
 * the SQL chart picker has always rendered -- the analytics KPI strip is the
 * only caller that passes it. */
export function KpiCard({
  label,
  value,
  format,
  title,
  delta,
  caption,
}: {
  label: string
  value: number
  format: NumberFormat
  title?: string
  delta?: number | null
  caption?: string
}) {
  const hasDelta = typeof delta === 'number' && Number.isFinite(delta)
  const deltaTone = !hasDelta ? '' : delta > 0 ? 'text-[var(--success)]' : delta < 0 ? 'text-[var(--danger)]' : 'text-[var(--muted-foreground)]'
  const deltaArrow = !hasDelta ? '' : delta > 0 ? '▲' : delta < 0 ? '▼' : '•'

  return (
    <div className="flex flex-col items-start gap-1 rounded-lg border border-[var(--border)] bg-[var(--card)] p-6">
      {title && <p className="text-sm font-semibold">{title}</p>}
      <p className="text-4xl font-semibold tabular-nums text-[var(--foreground)]">
        {formatNumber(value, format)}
      </p>
      <p className="text-sm text-[var(--muted-foreground)]">{label}</p>
      {hasDelta && (
        <p className={`text-sm font-medium tabular-nums ${deltaTone}`}>
          {deltaArrow} {Math.abs(delta).toFixed(1)}%
        </p>
      )}
      {caption && <p className="text-xs text-[var(--muted-foreground)]">{caption}</p>}
    </div>
  )
}
