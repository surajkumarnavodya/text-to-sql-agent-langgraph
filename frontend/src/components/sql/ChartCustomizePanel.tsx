import { useTranslation } from 'react-i18next'
import { Select } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import type { ChartOptions, ColumnInfo } from '@/lib/chartEngine'

const NONE = '__none__'

/** Compact set of controls for the currently-selected chart type -- axis/
 * measure columns, sort order, Top-N limit, date grouping (only offered
 * when the x-axis is a date column), number format, title, and legend
 * visibility. Every control is a plain, keyboard-operable native element
 * (<select>/<input>), matching this app's existing accessibility-first
 * convention (see components/ui/select.tsx's own docstring for why a
 * native select was chosen over a custom listbox everywhere else too). */
export function ChartCustomizePanel({
  columns,
  options,
  onChange,
}: {
  columns: ColumnInfo[]
  options: ChartOptions
  onChange: (next: ChartOptions) => void
}) {
  const { t } = useTranslation()
  const numericColumns = columns.filter((c) => c.role === 'numeric')
  const axisColumns = columns.filter((c) => c.role !== 'numeric')

  const isScatter = options.chartType === 'scatter'
  const isKpiOrTable = options.chartType === 'kpi' || options.chartType === 'table'
  const supportsSecondMeasure = options.chartType === 'bar-stacked' || options.chartType === 'mixed'
  const supportsStackingToggle = options.chartType === 'bar' || options.chartType === 'bar-horizontal'
  const xColumnRole = columns.find((c) => c.name === options.series.xColumn)?.role
  const supportsDateGrouping = xColumnRole === 'date'

  if (isKpiOrTable) return null

  const update = (patch: Partial<ChartOptions>) => onChange({ ...options, ...patch })
  const updateSeries = (patch: Partial<ChartOptions['series']>) =>
    onChange({ ...options, series: { ...options.series, ...patch } })

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <label className="flex flex-col gap-1 text-xs">
        <span className="font-medium text-[var(--muted-foreground)]">
          {isScatter ? t('chart.xMeasure') : t('chart.xAxis')}
        </span>
        <Select
          value={options.series.xColumn ?? NONE}
          onChange={(e) => updateSeries({ xColumn: e.target.value === NONE ? null : e.target.value })}
        >
          {!isScatter && <option value={NONE}>{t('chart.selectColumn')}</option>}
          {(isScatter ? numericColumns : axisColumns).map((col) => (
            <option key={col.name} value={col.name}>
              {col.name}
            </option>
          ))}
        </Select>
      </label>

      <label className="flex flex-col gap-1 text-xs">
        <span className="font-medium text-[var(--muted-foreground)]">
          {isScatter ? t('chart.yMeasure') : t('chart.yAxis')}
        </span>
        <Select
          value={options.series.yColumns[0] ?? NONE}
          onChange={(e) => {
            const first = e.target.value === NONE ? [] : [e.target.value]
            const rest = options.series.yColumns.slice(1)
            updateSeries({ yColumns: [...first, ...rest] })
          }}
        >
          <option value={NONE}>{t('chart.selectColumn')}</option>
          {numericColumns.map((col) => (
            <option key={col.name} value={col.name}>
              {col.name}
            </option>
          ))}
        </Select>
      </label>

      {supportsSecondMeasure && (
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-[var(--muted-foreground)]">{t('chart.additionalMeasure')}</span>
          <Select
            value={options.series.yColumns[1] ?? NONE}
            onChange={(e) => {
              const first = options.series.yColumns[0]
              const second = e.target.value === NONE ? [] : [e.target.value]
              updateSeries({ yColumns: first ? [first, ...second] : second })
            }}
          >
            <option value={NONE}>{t('chart.none')}</option>
            {numericColumns
              .filter((col) => col.name !== options.series.yColumns[0])
              .map((col) => (
                <option key={col.name} value={col.name}>
                  {col.name}
                </option>
              ))}
          </Select>
        </label>
      )}

      {!isScatter && (
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-[var(--muted-foreground)]">{t('chart.sortOrder')}</span>
          <Select
            value={options.sortOrder}
            onChange={(e) => update({ sortOrder: e.target.value as ChartOptions['sortOrder'] })}
          >
            <option value="none">{t('chart.sortNone')}</option>
            <option value="x-asc">{t('chart.sortXAsc')}</option>
            <option value="x-desc">{t('chart.sortXDesc')}</option>
            <option value="y-asc">{t('chart.sortYAsc')}</option>
            <option value="y-desc">{t('chart.sortYDesc')}</option>
          </Select>
        </label>
      )}

      {!isScatter && (
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-[var(--muted-foreground)]">{t('chart.topN')}</span>
          <Select
            value={options.topN == null ? NONE : String(options.topN)}
            onChange={(e) => update({ topN: e.target.value === NONE ? null : Number(e.target.value) })}
          >
            <option value={NONE}>{t('chart.topNAll')}</option>
            {[5, 10, 20, 50].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </Select>
        </label>
      )}

      {supportsDateGrouping && (
        <label className="flex flex-col gap-1 text-xs">
          <span className="font-medium text-[var(--muted-foreground)]">{t('chart.dateGrouping')}</span>
          <Select
            value={options.dateGrouping}
            onChange={(e) => update({ dateGrouping: e.target.value as ChartOptions['dateGrouping'] })}
          >
            <option value="none">{t('chart.dateGroupingNone')}</option>
            <option value="day">{t('chart.dateGroupingDay')}</option>
            <option value="week">{t('chart.dateGroupingWeek')}</option>
            <option value="month">{t('chart.dateGroupingMonth')}</option>
          </Select>
        </label>
      )}

      <label className="flex flex-col gap-1 text-xs">
        <span className="font-medium text-[var(--muted-foreground)]">{t('chart.numberFormat')}</span>
        <Select
          value={options.numberFormat}
          onChange={(e) => update({ numberFormat: e.target.value as ChartOptions['numberFormat'] })}
        >
          <option value="plain">{t('chart.formatPlain')}</option>
          <option value="currency">{t('chart.formatCurrency')}</option>
          <option value="percent">{t('chart.formatPercent')}</option>
        </Select>
      </label>

      <label className="flex flex-col gap-1 text-xs">
        <span className="font-medium text-[var(--muted-foreground)]">{t('chart.titleLabel')}</span>
        <input
          type="text"
          value={options.title}
          onChange={(e) => update({ title: e.target.value })}
          placeholder={t('chart.titlePlaceholder')}
          maxLength={120}
          className="h-9 rounded-md border border-[var(--border)] bg-[var(--input)] px-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        />
      </label>

      <label className="flex items-center justify-between gap-2 rounded-md border border-[var(--border)] px-2.5 text-xs">
        <span className="font-medium text-[var(--muted-foreground)]">{t('chart.showLegend')}</span>
        <Switch checked={options.showLegend} onCheckedChange={(checked) => update({ showLegend: checked })} />
      </label>

      {supportsStackingToggle && (
        <label className="flex items-center justify-between gap-2 rounded-md border border-[var(--border)] px-2.5 text-xs">
          <span className="font-medium text-[var(--muted-foreground)]">{t('chart.stacked')}</span>
          <Switch checked={options.stacked} onCheckedChange={(checked) => update({ stacked: checked })} />
        </label>
      )}
    </div>
  )
}
