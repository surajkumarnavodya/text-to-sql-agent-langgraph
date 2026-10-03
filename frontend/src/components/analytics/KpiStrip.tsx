import { KpiCard } from '@/components/sql/KpiCard'
import type { KpiItem } from '@/lib/analyticsCharts'

/** Headline KPI cards for one analytics result -- reuses the existing
 * `KpiCard` (extended in place with an optional delta/caption, so the SQL
 * chart picker's own single-value card is unchanged). */
export function KpiStrip({ kpis }: { kpis: KpiItem[] }) {
  if (kpis.length === 0) return null
  return (
    <ul aria-label="Key figures" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {kpis.map((kpi, index) => (
        <li key={`${kpi.label}-${index}`}>
          <KpiCard
            label={kpi.label}
            value={kpi.value}
            format="plain"
            delta={kpi.deltaPercent}
            caption={kpi.caption}
          />
        </li>
      ))}
    </ul>
  )
}
