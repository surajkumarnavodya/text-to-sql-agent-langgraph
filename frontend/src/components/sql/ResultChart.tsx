import {
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Filler,
  Legend,
  LinearScale,
  LineElement,
  PointElement,
  Title,
  Tooltip,
} from 'chart.js'
import { useMemo } from 'react'
import { Bar, Line } from 'react-chartjs-2'
import { useTranslation } from 'react-i18next'
import { adaptPlotlyFigure } from '@/lib/chartAdapter'
import type { PlotlyFigure } from '@/lib/types'
import { useSettingsStore } from '@/store/settingsStore'

ChartJS.register(CategoryScale, LinearScale, BarElement, LineElement, PointElement, Title, Tooltip, Legend, Filler)

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

/** Renders the backend's chart decision (see agent/result_charting.py) with
 * Chart.js -- tooltips, a themed gradient fill on line charts, rounded bar
 * corners, and full light/dark/accent-aware theming (colors are re-read
 * from the live CSS variables whenever the theme/accent settings change,
 * since a <canvas> can't reference `var(--x)` directly the way DOM
 * elements can). */
export function ResultChart({ figure }: { figure: PlotlyFigure }) {
  const { t } = useTranslation()
  // Subscribed only to force a re-render (and therefore a re-read of the
  // computed CSS variables below) when either setting changes -- the
  // values themselves aren't used directly.
  useSettingsStore((state) => state.themeMode)
  useSettingsStore((state) => state.accent)

  const adapted = useMemo(() => adaptPlotlyFigure(figure), [figure])

  const colors = useMemo(
    () => ({
      accent: cssVar('--accent') || '#4f46e5',
      accentSoft: cssVar('--accent-soft') || '#eef2ff',
      foreground: cssVar('--foreground') || '#0f172a',
      mutedForeground: cssVar('--muted-foreground') || '#64748b',
      border: cssVar('--border') || '#e2e8f0',
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentionally re-runs on theme/accent change via the subscriptions above
    [figure],
  )

  if (!adapted) return null

  const commonScales = {
    x: {
      title: { display: Boolean(adapted.xTitle), text: adapted.xTitle, color: colors.mutedForeground },
      ticks: { color: colors.mutedForeground },
      grid: { color: colors.border },
    },
    y: {
      title: { display: Boolean(adapted.yTitle), text: adapted.yTitle, color: colors.mutedForeground },
      ticks: { color: colors.mutedForeground },
      grid: { color: colors.border },
      beginAtZero: true,
    },
  }

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 500, easing: 'easeOutQuart' as const },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: colors.foreground,
        titleColor: colors.accentSoft,
        bodyColor: '#ffffff',
        padding: 10,
        cornerRadius: 8,
        displayColors: false,
      },
    },
    scales: commonScales,
  }

  const data = {
    labels: adapted.labels,
    datasets: [
      {
        label: adapted.yTitle || 'Value',
        data: adapted.values,
        backgroundColor: adapted.kind === 'bar' ? colors.accent : colors.accentSoft,
        borderColor: colors.accent,
        borderWidth: adapted.kind === 'line' ? 2.5 : 0,
        borderRadius: adapted.kind === 'bar' ? 6 : 0,
        pointRadius: adapted.kind === 'line' ? 3 : 0,
        pointBackgroundColor: colors.accent,
        fill: adapted.kind === 'line',
        tension: 0.35,
      },
    ],
  }

  return (
    <div className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold">📈 {t('results.chart')}</h3>
      <div className="rounded-md border border-[var(--border)] p-3" style={{ height: 320 }}>
        {adapted.kind === 'bar' ? <Bar data={data} options={options} /> : <Line data={data} options={options} />}
      </div>
    </div>
  )
}
