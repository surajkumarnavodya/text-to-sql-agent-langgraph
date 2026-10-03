import type { PreparedChart } from '@/lib/chartEngine'
import type { AnalyticsResult, ForecastResult, GrowthStat, RankingStat } from '@/lib/types'

/** Prompt 30 -- pure, DOM-free adapters from the backend's typed analytics
 * stats (`AnalyticsResult`, `ForecastResult`) to the existing `PreparedChart`
 * shape `ResultChart.tsx` already renders. No new chart library, no new
 * chart engine: the analytics panels reuse the same renderer the confirmed-
 * SQL chart picker uses (see frontend/src/lib/chartEngine.ts).
 *
 * Every label that originates in the database (a period, a category name, a
 * recommendation's own text) is untrusted data (master rule 8). Anything that
 * is shown, or fed back into a new question via `drillDownQuestion`, goes
 * through `sanitizeLabel` first -- control characters stripped, whitespace
 * collapsed, length capped. */

export const MAX_LABEL_CHARS = 60

function replaceControlCharacters(value: string): string {
  let out = ''
  for (const char of value) {
    const code = char.codePointAt(0) ?? 0
    out += code < 32 || code === 127 ? ' ' : char
  }
  return out
}

export function sanitizeLabel(value: string, max: number = MAX_LABEL_CHARS): string {
  const cleaned = replaceControlCharacters(value).replace(/\s+/g, ' ').trim()
  return cleaned.length > max ? `${cleaned.slice(0, max - 1)}…` : cleaned
}

/** The one-click drill-down question a ranking row submits. A fixed template
 * around two sanitized, length-capped labels -- never raw database text
 * spliced in unbounded. The question still goes through the normal `/ask`
 * path, so `agent.input_guard` and the SQL validator apply to it exactly as
 * they would to anything the user typed. */
export function drillDownQuestion(label: string, valueColumn: string): string {
  return `Show the details behind ${sanitizeLabel(label)} for ${sanitizeLabel(valueColumn)}`
}

export function growthToPrepared(growth: GrowthStat, color: string): PreparedChart {
  return {
    chartJsType: 'line',
    labels: growth.points.map((point) => sanitizeLabel(point.period)),
    datasets: [
      {
        label: sanitizeLabel(growth.value_column),
        data: growth.points.map((point) => point.value),
        color,
      },
    ],
    xTitle: sanitizeLabel(growth.label_column),
    yTitle: sanitizeLabel(growth.value_column),
    notices: [],
  }
}

/** The panels disclose truncation and estimate status in their own visible
 * text -- `ResultChart` does not render `PreparedChart.notices`, so these
 * adapters leave it empty rather than carry text nothing would show. */
export function rankingToPrepared(ranking: RankingStat, color: string, maxBars = 10): PreparedChart {
  const shown = ranking.entries.slice(0, maxBars)
  return {
    chartJsType: 'bar',
    labels: shown.map((entry) => sanitizeLabel(entry.label)),
    datasets: [
      {
        label: sanitizeLabel(ranking.value_column),
        data: shown.map((entry) => entry.value),
        color,
      },
    ],
    xTitle: sanitizeLabel(ranking.value_column),
    yTitle: sanitizeLabel(ranking.label_column),
    notices: [],
  }
}

/** A ranking viewed as its share-of-total breakdown (the backend's own
 * "a distribution is the same ranked breakdown viewed by its share-of-total
 * percentages" rule -- see analytics.models.RankingStat's docstring). Slices
 * beyond `maxSlices` collapse into one labeled "Other" slice, never dropped
 * silently. */
export function distributionToPrepared(ranking: RankingStat, color: string, maxSlices = 8): PreparedChart {
  const top = ranking.entries.slice(0, maxSlices)
  const rest = ranking.entries.slice(maxSlices)
  const slices = rest.length > 0
    ? [...top.map((e) => ({ label: sanitizeLabel(e.label), value: e.value })), {
        label: `Other (${rest.length})`,
        value: rest.reduce((sum, e) => sum + e.value, 0),
      }]
    : top.map((e) => ({ label: sanitizeLabel(e.label), value: e.value }))
  return {
    chartJsType: 'doughnut',
    labels: slices.map((slice) => slice.label),
    datasets: [{ label: sanitizeLabel(ranking.value_column), data: slices.map((slice) => slice.value), color }],
    xTitle: '',
    yTitle: sanitizeLabel(ranking.value_column),
    notices: [],
  }
}

export function forecastToPrepared(forecast: ForecastResult, color: string): PreparedChart {
  return {
    chartJsType: 'line',
    labels: forecast.points.map((point) => sanitizeLabel(point.period)),
    datasets: [{ label: 'Forecast', data: forecast.points.map((point) => point.forecast), color }],
    xTitle: 'Period',
    yTitle: 'Forecast',
    notices: [],
  }
}

export interface KpiItem {
  label: string
  value: number
  /** Percent change versus the previous period, or `null` when there's no
   * honest period-over-period comparison for this KPI. */
  deltaPercent: number | null
  caption: string
}

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/** Headline numbers for the KPI strip, derived only from already-computed
 * stats -- never a new calculation. A time series leads with its latest
 * value and overall change; a categorical breakdown leads with its total
 * (only when nothing was truncated, so the total is honest) and its top
 * entry; a scalar result shows its single value per numeric column. */
export function deriveKpis(result: AnalyticsResult): KpiItem[] {
  const kpis: KpiItem[] = []

  const growth = result.findings.find((finding) => finding.growth)?.growth
  if (growth && growth.points.length > 0) {
    const latest = growth.points[growth.points.length - 1]
    if (isFiniteNumber(latest.value)) {
      kpis.push({
        label: sanitizeLabel(growth.value_column),
        value: latest.value,
        deltaPercent: isFiniteNumber(growth.overall_change_percent) ? growth.overall_change_percent : null,
        caption: `Latest ${sanitizeLabel(latest.period)}`,
      })
    }
  }

  const ranking = result.findings.find((finding) => finding.ranking)?.ranking
  if (ranking && ranking.entries.length > 0) {
    const top = ranking.entries[0]
    if (!ranking.truncated) {
      const total = ranking.entries.reduce((sum, entry) => sum + entry.value, 0)
      if (isFiniteNumber(total)) {
        kpis.push({
          label: `Total ${sanitizeLabel(ranking.value_column)}`,
          value: total,
          deltaPercent: null,
          caption: `Across ${ranking.entries.length} ${sanitizeLabel(ranking.label_column)} values`,
        })
      }
    }
    if (isFiniteNumber(top.value)) {
      kpis.push({
        label: `Top ${sanitizeLabel(ranking.label_column)}`,
        value: top.value,
        deltaPercent: null,
        caption: top.share_percent === null
          ? sanitizeLabel(top.label)
          : `${sanitizeLabel(top.label)} · ${top.share_percent.toFixed(1)}% of total`,
      })
    }
  }

  if (result.shape === 'scalar') {
    for (const finding of result.findings) {
      const summary = finding.column_summary
      if (summary?.is_numeric && isFiniteNumber(summary.mean)) {
        kpis.push({
          label: sanitizeLabel(summary.column),
          value: summary.mean,
          deltaPercent: null,
          caption: 'Single result value',
        })
      }
    }
  }

  return kpis
}
