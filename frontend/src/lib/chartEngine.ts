/**
 * Client-side chart engine: column-role inference, per-type validity
 * checks, a recommendation, data transforms (sort/top-N/date grouping),
 * and Chart.js dataset/options builders.
 *
 * Deliberately fully client-side and independent of the backend's own
 * `chart_recommendation` (agent/result_charting.py) -- that field is only
 * ever used here as a *seed* for the initial axis selection, never trusted
 * as proof a chart type is valid. Every chart type's enabled/disabled state
 * is recomputed from the actual returned columns/rows every time, which is
 * also what lets the user switch chart types (or a follow-up question
 * change the visualization) without a server round trip.
 *
 * No dual-axis charts: per the data-viz method's own non-negotiable ("two
 * measures of different scale -> two charts, small multiples, or indexed to
 * a common base"), a "mixed" bar+line chart here shares one y-axis and is
 * only offered when the two measures are on a comparable scale -- never a
 * second right-hand axis.
 */

export type ColumnRole = 'numeric' | 'date' | 'text'

export interface ColumnInfo {
  name: string
  role: ColumnRole
}

export type ChartTypeId =
  | 'kpi'
  | 'bar'
  | 'bar-horizontal'
  | 'bar-stacked'
  | 'line'
  | 'area'
  | 'pie'
  | 'doughnut'
  | 'scatter'
  | 'mixed'
  | 'histogram'
  | 'table'

export const ALL_CHART_TYPES: ChartTypeId[] = [
  'kpi',
  'bar',
  'bar-horizontal',
  'bar-stacked',
  'line',
  'area',
  'pie',
  'doughnut',
  'scatter',
  'mixed',
  'histogram',
  'table',
]

export interface ChartTypeOption {
  type: ChartTypeId
  enabled: boolean
  /** Always present -- either why this type is a good fit, or why it's
   * disabled. Never leaves the user guessing. */
  reason: string
}

export interface ChartRecommendationHint {
  chart_type: string
  reason: string
  x_column: string | null
  y_column: string | null
}

export interface SeriesConfig {
  /** The category/date/x-axis column. Null for kpi (no axis) and for
   * scatter/mixed-with-two-measures-only shapes that use a numeric x. */
  xColumn: string | null
  /** One or more numeric measure columns. Most types use exactly one;
   * 'bar-stacked' and 'mixed' can use two or more. */
  yColumns: string[]
  /** An optional second categorical column to split bar/line series by
   * (e.g. "region" as x, "product_category" as the group) -- distinct from
   * using multiple yColumns, which stacks/groups different *measures*
   * rather than different *values of one column*. */
  groupColumn: string | null
}

export type SortOrder = 'none' | 'x-asc' | 'x-desc' | 'y-asc' | 'y-desc'
export type DateGrouping = 'none' | 'day' | 'week' | 'month'
export type NumberFormat = 'plain' | 'currency' | 'percent'

export interface ChartOptions {
  chartType: ChartTypeId
  series: SeriesConfig
  sortOrder: SortOrder
  topN: number | null
  dateGrouping: DateGrouping
  numberFormat: NumberFormat
  title: string
  showLegend: boolean
  stacked: boolean
}

export interface PreparedChart {
  /** Chart.js chart type this maps to -- several ChartTypeIds share one
   * underlying Chart.js type with different options (bar/bar-horizontal/
   * bar-stacked all render as chart.js "bar"). */
  chartJsType: 'bar' | 'line' | 'pie' | 'doughnut' | 'scatter'
  labels: (string | number)[]
  datasets: {
    label: string
    data: number[] | { x: number; y: number }[]
    color: string
    /** Only set for a 'mixed' chart -- overrides the dataset's own
     * chart.js type (bar vs. line) within one shared chart instance. */
    datasetType?: 'bar' | 'line'
  }[]
  xTitle: string
  yTitle: string
  /** User-visible disclosures about client-side transforms applied before
   * rendering -- e.g. "Showing top 10 of 42 rows by revenue" or "Grouped
   * by month". Never silent, per this feature's own requirement. */
  notices: string[]
}

// ---------------------------------------------------------------------------
// Column role inference
// ---------------------------------------------------------------------------

function parsesAsNumber(value: unknown): boolean {
  if (typeof value === 'number') return Number.isFinite(value)
  if (typeof value === 'string' && value.trim() !== '') return Number.isFinite(Number(value))
  return false
}

// `Date.parse`/`new Date(string)` is notoriously over-permissive (e.g.
// `Date.parse("Category 0")` succeeds, landing on an arbitrary date) -- a
// real bug caught by this module's own test suite. A candidate string must
// match one of these date-*shaped* patterns before `Date.parse` is ever
// trusted to interpret it, which is what actually rules out an ordinary
// category label.
const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/
const SLASH_OR_DOT_DATE_RE = /^\d{1,4}[/.]\d{1,2}[/.]\d{1,4}$/

function parsesAsDate(value: unknown): boolean {
  if (typeof value !== 'string') return false
  const trimmed = value.trim()
  if (trimmed === '') return false
  if (!ISO_DATE_RE.test(trimmed) && !SLASH_OR_DOT_DATE_RE.test(trimmed)) return false
  return !Number.isNaN(Date.parse(trimmed))
}

function inferRoleFromValues(name: string, values: unknown[]): ColumnRole {
  const nonNull = values.filter((v) => v !== null && v !== undefined)
  if (nonNull.length === 0) return 'text'

  if (nonNull.every(parsesAsNumber)) return 'numeric'

  const lowerName = name.toLowerCase()
  const looksDateByName = lowerName.includes('date') || lowerName.includes('time')
  if (looksDateByName && nonNull.some(parsesAsDate)) return 'date'
  if (nonNull.every(parsesAsDate)) return 'date'

  return 'text'
}

/** Infers each column's role. Prefers server-provided types
 * (`ExecuteResponse.column_types`, from `agent.result_charting
 * .classify_columns`'s pandas-dtype-based detection, more reliable than
 * guessing from JSON string values) and falls back to client-side
 * inference from the actual returned values when a column's server type is
 * missing or unrecognized.
 *
 * One deliberate exception to "prefer the server type": a server `"text"`
 * is re-checked against the actual values and overridden to `"numeric"`
 * when every one of them actually parses as a number. This is defense in
 * depth for a real, previously-shipped backend bug (a SQL Server
 * `DECIMAL`/`MONEY` column arrives via `pyodbc` as `decimal.Decimal`,
 * which pandas stores as `object` dtype -- `agent.result_charting
 * .classify_columns` has its own fix for this now, but this client-side
 * check keeps "Visualize" working correctly even against an older
 * backend, a stale cached response, or a future reclassification gap,
 * following this module's own "never trust a hint outright" principle
 * (already applied to `chart_recommendation`) consistently for
 * `column_types` too. Deliberately *not* applied to a server `"numeric"`/
 * `"date"` claim -- those are costlier to get "accidentally right" by
 * string-shape coincidence, and `inferColumnRoles`'s own test suite
 * already locks in "trust the server's numeric claim" for that
 * direction. */
export function inferColumnRoles(
  columns: string[],
  rows: unknown[][],
  serverColumnTypes?: Record<string, string>,
): ColumnInfo[] {
  return columns.map((name, index) => {
    const serverType = serverColumnTypes?.[name]
    const values = rows.map((row) => row[index])
    if (serverType === 'numeric' || serverType === 'date') {
      return { name, role: serverType }
    }
    if (serverType === 'text') {
      const nonNull = values.filter((v) => v !== null && v !== undefined)
      if (nonNull.length > 0 && nonNull.every(parsesAsNumber)) {
        return { name, role: 'numeric' }
      }
      return { name, role: 'text' }
    }
    return { name, role: inferRoleFromValues(name, values) }
  })
}

// ---------------------------------------------------------------------------
// Validity + recommendation
// ---------------------------------------------------------------------------

const PIE_MIN_CATEGORIES = 2
// Matches the data-viz method's categorical series-count ladder ("7-8:
// token ceiling; past it, fold the tail into 'Other' or facet") -- past
// this many slices, a pie/doughnut stops being readable and a bar chart
// (which has no such ceiling) is the honest alternative.
const PIE_MAX_CATEGORIES = 8
// A mixed bar+line chart shares one y-axis (see this module's own
// docstring) -- if the two measures' magnitudes differ by more than this
// ratio, one series would be visually flattened to a near-flat line, which
// is exactly the misleading-chart shape this feature must prevent.
const MIXED_SCALE_RATIO_LIMIT = 10

// A distribution needs at least this many points for binning to be
// meaningful -- mirrors analytics.visualization.build_chart_spec's own
// identical minimum (_MIN_ROWS_FOR_HISTOGRAM) on the backend, kept
// consistent rather than re-derived.
const MIN_ROWS_FOR_HISTOGRAM = 5
// Sturges' rule (ceil(log2(n) + 1)), clamped to a readable range -- too
// few buckets loses the shape of the distribution, too many produces bars
// too thin to read.
const MIN_HISTOGRAM_BUCKETS = 5
const MAX_HISTOGRAM_BUCKETS = 20

function columnsByRole(columns: ColumnInfo[], role: ColumnRole): ColumnInfo[] {
  return columns.filter((c) => c.role === role)
}

function numericValuesOf(rows: unknown[][], columns: string[], columnName: string): number[] {
  const index = columns.indexOf(columnName)
  if (index === -1) return []
  return rows
    .map((row) => row[index])
    .filter((v): v is number | string => v !== null && v !== undefined)
    .map(Number)
    .filter((n) => Number.isFinite(n))
}

function distinctValueCount(rows: unknown[][], columns: string[], columnName: string): number {
  const index = columns.indexOf(columnName)
  if (index === -1) return 0
  return new Set(rows.map((row) => String(row[index]))).size
}

/** Computes, for every supported chart type, whether it's a valid choice
 * for this exact result shape -- always from the actual columns/rows, never
 * from a suggestion. Each entry always carries a `reason`, whether enabled
 * or not, per this feature's "explain rather than silently disable"
 * requirement. */
export function getChartTypeOptions(
  columns: ColumnInfo[],
  rows: unknown[][],
): ChartTypeOption[] {
  const columnNames = columns.map((c) => c.name)
  const numericCols = columnsByRole(columns, 'numeric')
  const dateCols = columnsByRole(columns, 'date')
  const textCols = columnsByRole(columns, 'text')
  const categoryCols = [...dateCols, ...textCols]
  const rowCount = rows.length

  const options: ChartTypeOption[] = []

  // table -- always available, the safe fallback.
  options.push({ type: 'table', enabled: true, reason: 'Always available.' })

  // kpi -- exactly one row, at least one numeric column.
  if (rowCount === 0) {
    options.push({ type: 'kpi', enabled: false, reason: 'No rows to summarize.' })
  } else if (rowCount !== 1) {
    options.push({
      type: 'kpi',
      enabled: false,
      reason: `A stat card needs exactly one summary row; this result has ${rowCount}.`,
    })
  } else if (numericCols.length === 0) {
    options.push({ type: 'kpi', enabled: false, reason: 'Needs at least one numeric value.' })
  } else {
    options.push({ type: 'kpi', enabled: true, reason: 'A single row reads clearly as a stat card.' })
  }

  // bar / bar-horizontal -- a category or date axis plus a numeric measure.
  const barReasonIfDisabled = (): string | null => {
    if (rowCount === 0) return 'No rows to chart.'
    if (numericCols.length === 0) return 'Needs at least one numeric measure.'
    if (categoryCols.length === 0) return 'Needs a category or date column to compare across.'
    return null
  }
  const barDisabledReason = barReasonIfDisabled()
  for (const type of ['bar', 'bar-horizontal'] as const) {
    options.push(
      barDisabledReason
        ? { type, enabled: false, reason: barDisabledReason }
        : {
            type,
            enabled: true,
            reason: 'Compares a numeric measure across categories.',
          },
    )
  }

  // bar-stacked -- bar-valid, plus a second numeric measure to stack as a
  // segment (the customize panel offers "additional measures," not a
  // second grouping column -- so validity here matches exactly what the
  // UI can actually configure, never enabling a type it can't fulfill).
  if (barDisabledReason) {
    options.push({ type: 'bar-stacked', enabled: false, reason: barDisabledReason })
  } else if (numericCols.length < 2) {
    options.push({
      type: 'bar-stacked',
      enabled: false,
      reason: 'Stacking needs a second numeric measure to compare as segments.',
    })
  } else {
    options.push({
      type: 'bar-stacked',
      enabled: true,
      reason: 'Compares multiple measures within each category.',
    })
  }

  // line / area -- a numeric measure and at least 2 rows to show a trend.
  const lineReasonIfDisabled = (): string | null => {
    if (numericCols.length === 0) return 'Needs at least one numeric measure.'
    if (categoryCols.length === 0) return 'Needs a category or date column for the x-axis.'
    if (rowCount < 2) return 'A trend needs at least 2 rows.'
    return null
  }
  const lineDisabledReason = lineReasonIfDisabled()
  for (const type of ['line', 'area'] as const) {
    options.push(
      lineDisabledReason
        ? { type, enabled: false, reason: lineDisabledReason }
        : {
            type,
            enabled: true,
            reason:
              dateCols.length > 0
                ? 'Shows how the measure changes over time.'
                : 'Shows how the measure changes across ordered categories.',
          },
    )
  }

  // pie / doughnut -- one category column, one numeric measure, 2-8
  // distinct non-negative-summing categories, no duplicate categories
  // (duplicates would need silent summing to represent as one slice, which
  // this feature must never do).
  const pieDisabledReason = ((): string | null => {
    if (numericCols.length === 0) return 'Needs a numeric measure.'
    if (textCols.length === 0) return 'Needs a category column.'
    const categoryColumn = textCols[0].name
    const distinctCount = distinctValueCount(rows, columnNames, categoryColumn)
    if (distinctCount !== rowCount) {
      return 'Each category should appear once for a meaningful part-to-whole comparison; this result repeats some categories.'
    }
    if (rowCount < PIE_MIN_CATEGORIES) {
      return `Needs at least ${PIE_MIN_CATEGORIES} categories to compare as parts of a whole.`
    }
    if (rowCount > PIE_MAX_CATEGORIES) {
      return `Pie/doughnut charts work best with ${PIE_MIN_CATEGORIES}-${PIE_MAX_CATEGORIES} categories; this result has ${rowCount}.`
    }
    const values = numericValuesOf(rows, columnNames, numericCols[0].name)
    if (values.some((v) => v < 0)) {
      return 'Needs non-negative values to represent parts of a whole.'
    }
    if (values.every((v) => v === 0)) {
      return 'All values are zero -- there is no meaningful whole to divide.'
    }
    return null
  })()
  for (const type of ['pie', 'doughnut'] as const) {
    options.push(
      pieDisabledReason
        ? { type, enabled: false, reason: pieDisabledReason }
        : { type, enabled: true, reason: 'Shows each category’s share of the total.' },
    )
  }

  // scatter -- two numeric measures, at least 2 rows.
  if (numericCols.length < 2) {
    options.push({ type: 'scatter', enabled: false, reason: 'Needs two numeric measures.' })
  } else if (rowCount < 2) {
    options.push({ type: 'scatter', enabled: false, reason: 'Needs at least 2 rows.' })
  } else {
    options.push({
      type: 'scatter',
      enabled: true,
      reason: 'Reveals a relationship between two numeric measures.',
    })
  }

  // mixed (bar + line, one shared y-axis) -- 2+ numeric measures, a
  // category/date axis, and comparable scale between the two measures (see
  // MIXED_SCALE_RATIO_LIMIT's own comment for why).
  const mixedDisabledReason = ((): string | null => {
    if (numericCols.length < 2) return 'Needs two numeric measures.'
    if (categoryCols.length === 0) return 'Needs a category or date column for the x-axis.'
    const [first, second] = numericCols
    const firstValues = numericValuesOf(rows, columnNames, first.name).map(Math.abs)
    const secondValues = numericValuesOf(rows, columnNames, second.name).map(Math.abs)
    const firstMax = Math.max(0, ...firstValues)
    const secondMax = Math.max(0, ...secondValues)
    if (firstMax > 0 && secondMax > 0) {
      const ratio = Math.max(firstMax / secondMax, secondMax / firstMax)
      if (ratio > MIXED_SCALE_RATIO_LIMIT) {
        return 'The two measures have very different scales -- combining them on one axis would be misleading. Try two separate charts instead.'
      }
    }
    return null
  })()
  options.push(
    mixedDisabledReason
      ? { type: 'mixed', enabled: false, reason: mixedDisabledReason }
      : { type: 'mixed', enabled: true, reason: 'Compares two measures of similar scale together.' },
  )

  // histogram -- exactly one numeric column and NO category/date column
  // (binning computes its own x-axis purely from the numeric values) plus
  // enough rows for the bucketing to be meaningful. Mirrors
  // analytics.visualization.build_chart_spec's own identical rule (that
  // module's single-numeric branch is only reached once its date/text
  // branches have already been ruled out).
  if (numericCols.length !== 1 || textCols.length > 0 || dateCols.length > 0) {
    options.push({
      type: 'histogram',
      enabled: false,
      reason: 'Needs exactly one numeric column (no category or date column).',
    })
  } else if (rowCount < MIN_ROWS_FOR_HISTOGRAM) {
    options.push({
      type: 'histogram',
      enabled: false,
      reason: `Needs at least ${MIN_ROWS_FOR_HISTOGRAM} rows for a meaningful distribution.`,
    })
  } else {
    options.push({
      type: 'histogram',
      enabled: true,
      reason: 'Shows the distribution (shape and spread) of a single numeric column.',
    })
  }

  return options
}

/** A sensible default axis selection for `type`, independent of any prior
 * selection -- used both by `createDefaultChartOptions` (the very first
 * "Visualize" click) and whenever the user switches chart type in the
 * picker (switching types starts from a fresh, type-appropriate axis
 * choice rather than trying to awkwardly carry over an incompatible one,
 * e.g. a scatter's numeric x-axis surviving a switch to bar). */
export function defaultSeriesForType(type: ChartTypeId, columns: ColumnInfo[]): SeriesConfig {
  const numeric = columnsByRole(columns, 'numeric')
  const dateFirst = columnsByRole(columns, 'date')[0]
  const categoryOrDate = columns.filter((c) => c.role !== 'numeric')
  const axisColumn = (dateFirst ?? categoryOrDate[0])?.name ?? null

  if (type === 'kpi' || type === 'histogram') {
    // histogram, like kpi, has no x-axis column -- binning computes its
    // own x-axis from the single numeric column's own values.
    return { xColumn: null, yColumns: numeric.slice(0, 1).map((c) => c.name), groupColumn: null }
  }
  if (type === 'scatter') {
    return {
      xColumn: numeric[0]?.name ?? null,
      yColumns: numeric.slice(1, 2).map((c) => c.name),
      groupColumn: null,
    }
  }
  if (type === 'bar-stacked' || type === 'mixed') {
    return { xColumn: axisColumn, yColumns: numeric.slice(0, 2).map((c) => c.name), groupColumn: null }
  }
  return { xColumn: axisColumn, yColumns: numeric.slice(0, 1).map((c) => c.name), groupColumn: null }
}

/** Builds a complete, ready-to-preview `ChartOptions` -- the recommendation
 * when one validates, otherwise the first enabled non-table type with a
 * type-appropriate default axis selection. Returns `null` only when
 * nothing at all is chartable (the caller should already have checked this
 * via `getChartTypeOptions` before ever offering a "Visualize" action). */
export function createDefaultChartOptions(
  columns: ColumnInfo[],
  rows: unknown[][],
  backendHint?: ChartRecommendationHint | null,
): ChartOptions | null {
  const recommendation = recommendChart(columns, rows, backendHint)
  const chartType = recommendation?.type ?? getChartTypeOptions(columns, rows).find((o) => o.enabled && o.type !== 'table')?.type
  if (!chartType) return null

  return {
    chartType,
    series: recommendation?.series ?? defaultSeriesForType(chartType, columns),
    sortOrder: 'none',
    topN: rows.length > 20 ? 20 : null,
    dateGrouping: 'none',
    numberFormat: 'plain',
    title: '',
    showLegend: true,
    stacked: false,
  }
}

/** Suggests one chart type + a starting axis selection. The backend's own
 * `chart_recommendation` (if given) seeds the x/y column choice, but the
 * chart *type* it suggested is only used if this function's own validity
 * check (`getChartTypeOptions`) still confirms it's enabled for these exact
 * rows -- never trusted outright, per this feature's own requirement. */
export function recommendChart(
  columns: ColumnInfo[],
  rows: unknown[][],
  backendHint?: ChartRecommendationHint | null,
): { type: ChartTypeId; reason: string; series: SeriesConfig } | null {
  const options = getChartTypeOptions(columns, rows)
  const enabledByType = new Map(options.map((o) => [o.type, o]))
  const numericCols = columnsByRole(columns, 'numeric')
  const dateCols = columnsByRole(columns, 'date')
  const textCols = columnsByRole(columns, 'text')

  const tryType = (type: ChartTypeId): ChartTypeOption | null => {
    const option = enabledByType.get(type)
    return option?.enabled ? option : null
  }

  const hintType = backendHint?.chart_type as ChartTypeId | undefined
  if (hintType) {
    const validated = tryType(hintType)
    if (validated) {
      return {
        type: hintType,
        reason: validated.reason,
        series: {
          xColumn: backendHint?.x_column ?? null,
          yColumns: backendHint?.y_column ? [backendHint.y_column] : [],
          groupColumn: null,
        },
      }
    }
  }

  const kpi = tryType('kpi')
  if (kpi) return { type: 'kpi', reason: kpi.reason, series: { xColumn: null, yColumns: [numericCols[0]?.name].filter(Boolean), groupColumn: null } }

  const line = tryType('line')
  if (line && dateCols.length > 0) {
    return {
      type: 'line',
      reason: line.reason,
      series: { xColumn: dateCols[0].name, yColumns: [numericCols[0].name], groupColumn: null },
    }
  }

  const bar = tryType('bar')
  if (bar && textCols.length > 0) {
    return {
      type: 'bar',
      reason: bar.reason,
      series: { xColumn: textCols[0].name, yColumns: [numericCols[0].name], groupColumn: null },
    }
  }

  const scatter = tryType('scatter')
  if (scatter) {
    return {
      type: 'scatter',
      reason: scatter.reason,
      series: { xColumn: numericCols[0].name, yColumns: [numericCols[1].name], groupColumn: null },
    }
  }

  const histogram = tryType('histogram')
  if (histogram) {
    return {
      type: 'histogram',
      reason: histogram.reason,
      series: { xColumn: null, yColumns: [numericCols[0].name], groupColumn: null },
    }
  }

  return null
}

// ---------------------------------------------------------------------------
// Data preparation (sort / top-N / date grouping) -- all explicit, all disclosed
// ---------------------------------------------------------------------------

function formatDateForGrouping(value: unknown, grouping: DateGrouping): string {
  const date = new Date(String(value))
  if (Number.isNaN(date.getTime())) return String(value)
  if (grouping === 'day') return date.toISOString().slice(0, 10)
  if (grouping === 'month') return date.toISOString().slice(0, 7)
  if (grouping === 'week') {
    const dayOfWeek = date.getUTCDay()
    const weekStart = new Date(date)
    weekStart.setUTCDate(date.getUTCDate() - dayOfWeek)
    return weekStart.toISOString().slice(0, 10)
  }
  return String(value)
}

const DEFAULT_COLORS = [
  'var(--chart-cat-1)',
  'var(--chart-cat-2)',
  'var(--chart-cat-3)',
  'var(--chart-cat-4)',
  'var(--chart-cat-5)',
  'var(--chart-cat-6)',
]

export function resolveChartColors(): string[] {
  if (typeof document === 'undefined') return DEFAULT_COLORS
  const styles = getComputedStyle(document.documentElement)
  return DEFAULT_COLORS.map((token) => {
    const varName = token.slice(4, -1) // 'var(--x)' -> '--x'
    return styles.getPropertyValue(varName).trim() || token
  })
}

/** Bins `values` into a deterministic number of equal-width buckets
 * (Sturges' rule, clamped to [MIN_HISTOGRAM_BUCKETS, MAX_HISTOGRAM_BUCKETS])
 * and returns each bucket's label (its numeric range) and count. A
 * histogram is just a bar chart of these counts -- no new charting
 * library needed, reusing Chart.js's existing 'bar' rendering path
 * exactly like bar/bar-horizontal/bar-stacked already do. */
export function computeHistogramBins(values: number[]): { label: string; count: number }[] {
  if (values.length === 0) return []
  const min = Math.min(...values)
  const max = Math.max(...values)
  if (min === max) {
    return [{ label: formatNumber(min, 'plain'), count: values.length }]
  }
  const bucketCount = Math.min(
    MAX_HISTOGRAM_BUCKETS,
    Math.max(MIN_HISTOGRAM_BUCKETS, Math.ceil(Math.log2(values.length) + 1)),
  )
  const binWidth = (max - min) / bucketCount
  const counts = new Array(bucketCount).fill(0)
  for (const value of values) {
    // The maximum value would otherwise land one bucket past the end
    // (floor((max - min) / binWidth) === bucketCount) -- clamp it into
    // the last bucket instead, which is the inclusive-upper-bound
    // convention every histogram implementation uses.
    const index = Math.min(bucketCount - 1, Math.max(0, Math.floor((value - min) / binWidth)))
    counts[index] += 1
  }
  return counts.map((count, i) => {
    const rangeStart = min + i * binWidth
    const rangeEnd = min + (i + 1) * binWidth
    return {
      label: `${formatNumber(rangeStart, 'plain')}–${formatNumber(rangeEnd, 'plain')}`,
      count,
    }
  })
}

/** Builds the Chart.js-ready shape for one chart configuration, applying
 * (and disclosing, via `notices`) any sort/top-N/date-grouping transforms
 * the user requested. Returns `null` if the requested type/column
 * combination doesn't actually validate against the data (defense in depth
 * -- the picker UI should never let this happen, but this function never
 * trusts its caller either). Never aggregates/sums duplicate x-values --
 * grouping by date bucket is the one explicit exception, and only when the
 * user explicitly requests it (`dateGrouping !== 'none'`), never automatic. */
export function prepareChart(
  columns: ColumnInfo[],
  rows: unknown[][],
  options: ChartOptions,
  colors: string[] = resolveChartColors(),
): PreparedChart | null {
  const columnNames = columns.map((c) => c.name)
  const notices: string[] = []
  const { series } = options

  if (options.chartType === 'table' || options.chartType === 'kpi') return null

  if (series.yColumns.length === 0) return null

  // --- scatter: x and y are both numeric measures, one point per row ---
  if (options.chartType === 'scatter') {
    const xIndex = columnNames.indexOf(series.xColumn ?? '')
    const yIndex = columnNames.indexOf(series.yColumns[0])
    if (xIndex === -1 || yIndex === -1) return null
    const points = rows
      .map((row) => ({ x: Number(row[xIndex]), y: Number(row[yIndex]) }))
      .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y))
    return {
      chartJsType: 'scatter',
      labels: [],
      datasets: [{ label: `${series.yColumns[0]} vs ${series.xColumn}`, data: points, color: colors[0] }],
      xTitle: series.xColumn ?? '',
      yTitle: series.yColumns[0],
      notices,
    }
  }

  // --- histogram: bin the single numeric column's own values, no x-axis
  // column needed (see computeHistogramBins's own docstring) ---
  if (options.chartType === 'histogram') {
    const yIndex = columnNames.indexOf(series.yColumns[0])
    if (yIndex === -1) return null
    const values = rows
      .map((row) => Number(row[yIndex]))
      .filter((n) => Number.isFinite(n))
    const bins = computeHistogramBins(values)
    return {
      chartJsType: 'bar',
      labels: bins.map((b) => b.label),
      datasets: [{ label: series.yColumns[0], data: bins.map((b) => b.count), color: colors[0] }],
      xTitle: series.yColumns[0],
      yTitle: 'Count',
      notices,
    }
  }

  const xColumn = series.xColumn
  if (!xColumn) return null
  const xIndex = columnNames.indexOf(xColumn)
  if (xIndex === -1) return null

  // Build working rows: [xLabel, ...yValues], optionally date-bucketed.
  let working = rows.map((row) => {
    const rawX = row[xIndex]
    const xLabel = options.dateGrouping !== 'none' ? formatDateForGrouping(rawX, options.dateGrouping) : String(rawX)
    return { xLabel, row }
  })

  if (options.dateGrouping !== 'none') {
    notices.push(`Dates grouped by ${options.dateGrouping}.`)
  }

  // Sort
  if (options.sortOrder !== 'none') {
    const yIndexForSort = columnNames.indexOf(series.yColumns[0])
    working = [...working].sort((a, b) => {
      if (options.sortOrder === 'x-asc') return a.xLabel < b.xLabel ? -1 : a.xLabel > b.xLabel ? 1 : 0
      if (options.sortOrder === 'x-desc') return a.xLabel > b.xLabel ? -1 : a.xLabel < b.xLabel ? 1 : 0
      const aVal = Number(a.row[yIndexForSort]) || 0
      const bVal = Number(b.row[yIndexForSort]) || 0
      return options.sortOrder === 'y-asc' ? aVal - bVal : bVal - aVal
    })
  }

  // Top-N -- explicit, disclosed, never silent.
  if (options.topN != null && options.topN > 0 && working.length > options.topN) {
    const total = working.length
    working = working.slice(0, options.topN)
    notices.push(`Showing top ${options.topN} of ${total} rows.`)
  }

  const labels = working.map((w) => w.xLabel)

  const buildSeries = (yColumn: string, colorIndex: number, datasetType?: 'bar' | 'line') => {
    const yIndex = columnNames.indexOf(yColumn)
    return {
      label: yColumn,
      data: working.map((w) => Number(w.row[yIndex]) || 0),
      color: colors[colorIndex % colors.length],
      ...(datasetType ? { datasetType } : {}),
    }
  }

  let chartJsType: PreparedChart['chartJsType'] = 'bar'
  let datasets: PreparedChart['datasets']

  switch (options.chartType) {
    case 'bar':
    case 'bar-horizontal':
      chartJsType = 'bar'
      datasets = series.yColumns.map((col, i) => buildSeries(col, i))
      break
    case 'bar-stacked':
      chartJsType = 'bar'
      datasets = series.yColumns.map((col, i) => buildSeries(col, i))
      break
    case 'line':
      chartJsType = 'line'
      datasets = series.yColumns.map((col, i) => buildSeries(col, i))
      break
    case 'area':
      chartJsType = 'line'
      datasets = series.yColumns.map((col, i) => buildSeries(col, i))
      break
    case 'pie':
    case 'doughnut':
      chartJsType = options.chartType
      datasets = [
        {
          label: series.yColumns[0],
          data: working.map((w) => Number(w.row[columnNames.indexOf(series.yColumns[0])]) || 0),
          color: colors[0],
        },
      ]
      break
    case 'mixed':
      chartJsType = 'bar'
      datasets = series.yColumns.map((col, i) => buildSeries(col, i, i === 0 ? 'bar' : 'line'))
      break
    default:
      return null
  }

  return {
    chartJsType,
    labels,
    datasets,
    xTitle: xColumn,
    yTitle: series.yColumns.join(', '),
    notices,
  }
}

// ---------------------------------------------------------------------------
// Display formatting (never touches the raw values used for the chart math)
// ---------------------------------------------------------------------------

export function formatNumber(value: number, format: NumberFormat): string {
  if (format === 'currency') {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(value)
  }
  if (format === 'percent') {
    return new Intl.NumberFormat(undefined, { style: 'percent', maximumFractionDigits: 1 }).format(value)
  }
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value)
}

export const CHART_TYPE_LABELS: Record<ChartTypeId, string> = {
  kpi: 'Stat card',
  bar: 'Bar',
  'bar-horizontal': 'Horizontal bar',
  'bar-stacked': 'Stacked bar',
  line: 'Line',
  area: 'Area',
  pie: 'Pie',
  doughnut: 'Doughnut',
  scatter: 'Scatter',
  mixed: 'Mixed bar + line',
  histogram: 'Histogram',
  table: 'Table only',
}
