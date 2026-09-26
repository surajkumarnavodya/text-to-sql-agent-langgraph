import { formatNumber, type NumberFormat } from '@/lib/chartEngine'

/** A single-value stat card -- the "one numeric aggregate" chart type
 * (never a one-bar bar chart, per this feature's own chart-type rules).
 * Deliberately not a Chart.js canvas -- a hero number reads clearer as
 * plain typography than as a "chart" with one data point. */
export function KpiCard({
  label,
  value,
  format,
  title,
}: {
  label: string
  value: number
  format: NumberFormat
  title?: string
}) {
  return (
    <div className="flex flex-col items-start gap-1 rounded-lg border border-[var(--border)] bg-[var(--card)] p-6">
      {title && <p className="text-sm font-semibold">{title}</p>}
      <p className="text-4xl font-semibold tabular-nums text-[var(--foreground)]">
        {formatNumber(value, format)}
      </p>
      <p className="text-sm text-[var(--muted-foreground)]">{label}</p>
    </div>
  )
}
