import { AlertTriangle, BarChart2, Settings2, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '@/components/ui/button'
import {
  createDefaultChartOptions,
  defaultSeriesForType,
  getChartTypeOptions,
  inferColumnRoles,
  prepareChart,
  type ChartOptions,
  type ChartRecommendationHint,
  type ChartTypeId,
} from '@/lib/chartEngine'
import { ChartCustomizePanel } from './ChartCustomizePanel'
import { ChartPicker } from './ChartPicker'
import { KpiCard } from './KpiCard'
import { ResultChart } from './ResultChart'

/** Owns the entire optional-visualization flow for one confirmed SQL
 * result: an unobtrusive "Visualize" action (never a chart shown by
 * default); once clicked, a chart-type picker with a live preview and an
 * explicit "Generate chart" step; once generated, "Customize" (reopens the
 * same picker/panel, seeded from the current config) and "Remove chart"
 * (drops back to just the answer + table, never the other way around).
 *
 * Every chart type's enabled/disabled state and every rendered value comes
 * from `chartEngine.ts`, computed fresh from the actual `columns`/`rows`
 * every render -- never from the LLM/backend's own recommendation taken at
 * face value (see that module's docstring). */
export function ChartSection({
  columns,
  rows,
  columnTypes,
  chartRecommendation,
  truncated,
  chartOptions,
  onChartOptionsChange,
}: {
  columns: string[]
  rows: unknown[][]
  columnTypes: Record<string, string>
  chartRecommendation: ChartRecommendationHint | null
  truncated: boolean
  chartOptions: ChartOptions | null
  onChartOptionsChange: (options: ChartOptions | null) => void
}) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState<ChartOptions | null>(null)

  const columnInfos = useMemo(() => inferColumnRoles(columns, rows, columnTypes), [columns, rows, columnTypes])
  const chartTypeOptions = useMemo(() => getChartTypeOptions(columnInfos, rows), [columnInfos, rows])
  const hasAnyChartableType = chartTypeOptions.some((o) => o.enabled && o.type !== 'table')

  const activeOptions = draft ?? chartOptions
  const prepared = useMemo(
    () => (activeOptions ? prepareChart(columnInfos, rows, activeOptions) : null),
    [activeOptions, columnInfos, rows],
  )

  // Nothing to visualize at all -- an empty result, a single unlabelled
  // text column, etc. Per this feature's own requirement, the action is
  // disabled with a reason rather than hidden entirely, so the user isn't
  // left wondering whether visualization was simply forgotten.
  if (rows.length === 0) return null

  const openEditor = () => setDraft(chartOptions ?? createDefaultChartOptions(columnInfos, rows, chartRecommendation))
  const cancelEditing = () => setDraft(null)
  const generate = () => {
    if (draft) onChartOptionsChange(draft)
    setDraft(null)
  }
  const removeChart = () => {
    onChartOptionsChange(null)
    setDraft(null)
  }
  const resetToRecommended = () =>
    setDraft(createDefaultChartOptions(columnInfos, rows, chartRecommendation))

  const changeType = (type: ChartTypeId) => {
    if (!draft) return
    setDraft({ ...draft, chartType: type, series: defaultSeriesForType(type, columnInfos) })
  }

  const renderPreview = (opts: ChartOptions) => {
    if (opts.chartType === 'kpi') {
      const yColumn = opts.series.yColumns[0]
      const index = columns.indexOf(yColumn)
      const value = index >= 0 ? Number(rows[0]?.[index]) : NaN
      if (!Number.isFinite(value)) {
        return <p className="text-sm text-[var(--muted-foreground)]">{t('chart.selectValidColumns')}</p>
      }
      return <KpiCard label={yColumn} value={value} format={opts.numberFormat} title={opts.title || undefined} />
    }
    if (!prepared) {
      return <p className="text-sm text-[var(--muted-foreground)]">{t('chart.selectValidColumns')}</p>
    }
    return (
      <ResultChart
        prepared={prepared}
        chartType={opts.chartType}
        title={opts.title}
        showLegend={opts.showLegend}
        numberFormat={opts.numberFormat}
        stacked={opts.stacked}
      />
    )
  }

  const truncationNotice = truncated && (
    <p className="flex items-center gap-1.5 text-xs text-[var(--warning)]">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      {t('chart.truncatedNotice')}
    </p>
  )

  // --- Editing (picker + customize + live preview), first pick or later "Customize" ---
  if (draft) {
    return (
      <div className="flex flex-col gap-3 rounded-lg border border-[var(--border)] bg-[var(--card)] p-3">
        <ChartPicker options={chartTypeOptions} value={draft.chartType} onChange={changeType} />
        <ChartCustomizePanel columns={columnInfos} options={draft} onChange={setDraft} />
        {truncationNotice}
        {prepared?.notices.map((notice) => (
          <p key={notice} className="text-xs text-[var(--muted-foreground)]">
            {notice}
          </p>
        ))}
        <div className="rounded-md border border-[var(--border)] p-3" style={{ minHeight: 280 }}>
          {renderPreview(draft)}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={resetToRecommended}>
            {t('chart.resetToRecommended')}
          </Button>
          <Button size="sm" variant="secondary" onClick={cancelEditing}>
            {t('common.cancel')}
          </Button>
          <Button size="sm" variant="primary" onClick={generate}>
            {t('chart.generate')}
          </Button>
        </div>
      </div>
    )
  }

  // --- A chart already exists -- show it plus Customize/Remove ---
  if (chartOptions) {
    return (
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between gap-2">
          <h3 className="flex items-center gap-1.5 text-sm font-semibold">
            <BarChart2 className="h-4 w-4 text-[var(--muted-foreground)]" />
            {t('results.chart')}
          </h3>
          <div className="flex items-center gap-1.5">
            <Button size="sm" variant="secondary" onClick={openEditor}>
              <Settings2 className="h-3.5 w-3.5" />
              {t('chart.customize')}
            </Button>
            <Button size="sm" variant="ghost" onClick={removeChart} aria-label={t('chart.removeChart')}>
              <X className="h-3.5 w-3.5" />
              {t('chart.removeChart')}
            </Button>
          </div>
        </div>
        {truncationNotice}
        {prepared?.notices.map((notice) => (
          <p key={notice} className="text-xs text-[var(--muted-foreground)]">
            {notice}
          </p>
        ))}
        <div className="rounded-md border border-[var(--border)] p-3" style={{ minHeight: 280 }}>
          {renderPreview(chartOptions)}
        </div>
      </div>
    )
  }

  // --- Nothing generated yet -- an unobtrusive opt-in action ---
  return (
    <Button
      size="sm"
      variant="secondary"
      onClick={openEditor}
      disabled={!hasAnyChartableType}
      title={hasAnyChartableType ? undefined : t('chart.noChartableShape')}
      className="w-fit"
    >
      <BarChart2 className="h-3.5 w-3.5" />
      {t('chart.visualize')}
    </Button>
  )
}
