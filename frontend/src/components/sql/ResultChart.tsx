import {
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Filler,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PieController,
  PointElement,
  ScatterController,
  Title,
  Tooltip,
  type ChartData,
  type ChartOptions as ChartJsOptions,
} from 'chart.js'
import { useMemo } from 'react'
import { Chart } from 'react-chartjs-2'
import { formatNumber, type ChartTypeId, type NumberFormat, type PreparedChart } from '@/lib/chartEngine'
import { useSettingsStore } from '@/store/settingsStore'

ChartJS.register(
  BarController,
  LineController,
  PieController,
  ScatterController,
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  ArcElement,
  Title,
  Tooltip,
  Legend,
  Filler,
)

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/** Renders one prepared chart configuration (see `frontend/src/lib
 * /chartEngine.ts`) with Chart.js, via `react-chartjs-2`'s generic `Chart`
 * component so one component can switch between every supported chart
 * type (bar/horizontal-bar/stacked-bar/line/area/pie/doughnut/scatter/
 * mixed) rather than needing a separate component per type. Colors/theming
 * are re-read from the live CSS variables whenever the theme/accent
 * settings change, since a `<canvas>` can't reference `var(--x)` directly
 * the way DOM elements can (same pattern this component already used
 * before this feature). */
export function ResultChart({
  prepared,
  chartType,
  title,
  showLegend,
  numberFormat,
  stacked,
}: {
  prepared: PreparedChart
  chartType: ChartTypeId
  title: string
  showLegend: boolean
  numberFormat: NumberFormat
  stacked: boolean
}) {
  // Subscribed only to force a re-render (and therefore a re-read of the
  // computed CSS variables below) when either setting changes.
  useSettingsStore((state) => state.themeMode)
  useSettingsStore((state) => state.accent)

  const colors = useMemo(
    () => ({
      foreground: cssVar('--foreground') || '#0f172a',
      mutedForeground: cssVar('--muted-foreground') || '#64748b',
      border: cssVar('--border') || '#e2e8f0',
      accentSoft: cssVar('--accent-soft') || '#eef2ff',
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentionally re-runs on theme/accent change via the subscriptions above
    [prepared],
  )

  const isHorizontal = chartType === 'bar-horizontal'
  const isArea = chartType === 'area'
  const isPieLike = prepared.chartJsType === 'pie' || prepared.chartJsType === 'doughnut'

  const data: ChartData = useMemo(() => {
    if (isPieLike) {
      const sliceColors = prepared.labels.map((_, i) => {
        const paletteIndex = (i % 6) + 1
        return cssVar(`--chart-cat-${paletteIndex}`) || prepared.datasets[0].color
      })
      return {
        labels: prepared.labels,
        datasets: [
          {
            label: prepared.datasets[0].label,
            data: prepared.datasets[0].data as number[],
            backgroundColor: sliceColors,
            borderColor: cssVar('--card') || '#ffffff',
            borderWidth: 2,
          },
        ],
      }
    }

    if (prepared.chartJsType === 'scatter') {
      return {
        datasets: prepared.datasets.map((ds) => ({
          label: ds.label,
          data: ds.data as { x: number; y: number }[],
          backgroundColor: ds.color,
          borderColor: ds.color,
          pointRadius: 5,
        })),
      }
    }

    return {
      labels: prepared.labels,
      datasets: prepared.datasets.map((ds) => {
        const type = ds.datasetType ?? (prepared.chartJsType === 'line' ? 'line' : 'bar')
        return {
          type,
          label: ds.label,
          data: ds.data as number[],
          backgroundColor: type === 'bar' ? ds.color : colors.accentSoft,
          borderColor: ds.color,
          borderWidth: type === 'line' ? 2.5 : 0,
          borderRadius: type === 'bar' ? 6 : 0,
          pointRadius: type === 'line' ? 3 : 0,
          pointBackgroundColor: ds.color,
          fill: type === 'line' && isArea,
          tension: 0.35,
        }
      }),
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prepared, isPieLike, isArea, colors])

  const options: ChartJsOptions = useMemo(() => {
    const tooltipFormat = (value: unknown) =>
      typeof value === 'number' ? formatNumber(value, numberFormat) : String(value)

    const base: ChartJsOptions = {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 400, easing: 'easeOutQuart' },
      plugins: {
        title: title ? { display: true, text: title, color: colors.foreground } : { display: false },
        legend: {
          display: showLegend && (isPieLike || prepared.datasets.length > 1),
          labels: { color: colors.mutedForeground },
        },
        tooltip: {
          backgroundColor: colors.foreground,
          padding: 10,
          cornerRadius: 8,
          callbacks: {
            label: (context) => {
              const raw = context.raw
              if (raw && typeof raw === 'object' && 'y' in raw) {
                return `${context.dataset.label}: ${tooltipFormat((raw as { y: number }).y)}`
              }
              return `${context.dataset.label}: ${tooltipFormat(context.parsed?.y ?? context.parsed)}`
            },
          },
        },
      },
    }

    if (isPieLike) return base

    if (prepared.chartJsType === 'scatter') {
      return {
        ...base,
        scales: {
          x: {
            type: 'linear',
            title: { display: Boolean(prepared.xTitle), text: prepared.xTitle, color: colors.mutedForeground },
            ticks: { color: colors.mutedForeground },
            grid: { color: colors.border },
          },
          y: {
            title: { display: Boolean(prepared.yTitle), text: prepared.yTitle, color: colors.mutedForeground },
            ticks: { color: colors.mutedForeground },
            grid: { color: colors.border },
          },
        },
      }
    }

    return {
      ...base,
      indexAxis: isHorizontal ? 'y' : 'x',
      scales: {
        x: {
          stacked: stacked || chartType === 'bar-stacked',
          title: {
            display: Boolean(!isHorizontal ? prepared.xTitle : prepared.yTitle),
            text: !isHorizontal ? prepared.xTitle : prepared.yTitle,
            color: colors.mutedForeground,
          },
          ticks: {
            color: colors.mutedForeground,
            callback: isHorizontal ? (value) => tooltipFormat(value) : undefined,
          },
          grid: { color: colors.border },
        },
        y: {
          stacked: stacked || chartType === 'bar-stacked',
          beginAtZero: true,
          title: {
            display: Boolean(!isHorizontal ? prepared.yTitle : prepared.xTitle),
            text: !isHorizontal ? prepared.yTitle : prepared.xTitle,
            color: colors.mutedForeground,
          },
          ticks: {
            color: colors.mutedForeground,
            callback: !isHorizontal ? (value) => tooltipFormat(value) : undefined,
          },
          grid: { color: colors.border },
        },
      },
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prepared, isHorizontal, isPieLike, stacked, chartType, title, showLegend, colors, numberFormat])

  // `react-chartjs-2`'s own `<canvas>` already carries `role="img"` by
  // default -- a wrapping div with its own `role="img"` would give the
  // chart two competing accessible-image nodes for the same content, which
  // is what `getByRole('img')` (rightly) treats as ambiguous. The
  // accessible name is set directly on the canvas itself instead (spread
  // into `canvasProps` by `react-chartjs-2`, landing on the one real node).
  return (
    <div style={{ height: 340 }}>
      <Chart type={prepared.chartJsType} data={data} options={options} aria-label={title || `${chartType} chart`} />
    </div>
  )
}
