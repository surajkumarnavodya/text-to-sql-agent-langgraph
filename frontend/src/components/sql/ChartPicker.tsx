import {
  AreaChart,
  BarChart2,
  BarChartHorizontal,
  Gauge,
  LineChart,
  PieChart,
  ScatterChart,
  Table2,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'
import { CHART_TYPE_LABELS, type ChartTypeId, type ChartTypeOption } from '@/lib/chartEngine'

const ICONS: Record<ChartTypeId, typeof BarChart2> = {
  kpi: Gauge,
  bar: BarChart2,
  'bar-horizontal': BarChartHorizontal,
  'bar-stacked': BarChart2,
  line: LineChart,
  area: AreaChart,
  pie: PieChart,
  doughnut: PieChart,
  scatter: ScatterChart,
  mixed: BarChart2,
  histogram: BarChart2,
  table: Table2,
}

/** A grid of chart-type options -- every type always renders (never hides
 * an unsuitable one outright), so the user can see what exists and why it
 * isn't available right now, per this feature's own "explain rather than
 * silently disable" requirement. The reason for the *currently selected*
 * type is always shown as visible text below the grid (not just a hover
 * tooltip), so it's readable on touch devices and by screen readers. */
export function ChartPicker({
  options,
  value,
  onChange,
}: {
  options: ChartTypeOption[]
  value: ChartTypeId
  onChange: (type: ChartTypeId) => void
}) {
  const { t } = useTranslation()
  const selectable = options.filter((o) => o.type !== 'table')
  const activeOption = options.find((o) => o.type === value)

  return (
    <div className="flex flex-col gap-2">
      <div
        role="radiogroup"
        aria-label={t('chart.pickerLabel')}
        className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5"
      >
        {selectable.map((option) => {
          const Icon = ICONS[option.type]
          const isSelected = option.type === value
          return (
            <button
              key={option.type}
              type="button"
              role="radio"
              aria-checked={isSelected}
              disabled={!option.enabled}
              title={option.reason}
              onClick={() => option.enabled && onChange(option.type)}
              className={cn(
                'flex flex-col items-center gap-1 rounded-lg border p-2.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--focus-ring)]',
                isSelected
                  ? 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--accent)]'
                  : 'border-[var(--border)] hover:bg-[var(--muted)]',
                !option.enabled && 'cursor-not-allowed opacity-40 hover:bg-transparent',
              )}
            >
              <Icon className="h-4 w-4" />
              <span className="text-center leading-tight">{CHART_TYPE_LABELS[option.type]}</span>
            </button>
          )
        })}
      </div>
      {activeOption && (
        <p className="text-xs text-[var(--muted-foreground)]">{activeOption.reason}</p>
      )}
    </div>
  )
}
