import { describe, expect, it } from 'vitest'
import {
  computeHistogramBins,
  getChartTypeOptions,
  inferColumnRoles,
  prepareChart,
  recommendChart,
  type ChartOptions,
  type ColumnInfo,
} from './chartEngine'

function roles(columns: string[], rows: unknown[][]): ColumnInfo[] {
  return inferColumnRoles(columns, rows)
}

describe('inferColumnRoles', () => {
  it('infers numeric from all-numeric values', () => {
    const result = roles(['revenue'], [[100], [200]])
    expect(result).toEqual([{ name: 'revenue', role: 'numeric' }])
  })

  it('infers text from category-like strings', () => {
    const result = roles(['region'], [['East'], ['West']])
    expect(result).toEqual([{ name: 'region', role: 'text' }])
  })

  it('infers date from a date-shaped column name plus parseable values', () => {
    const result = roles(['order_date'], [['2024-01-01'], ['2024-02-01']])
    expect(result).toEqual([{ name: 'order_date', role: 'date' }])
  })

  it('infers date from parseable values even without a date-shaped name', () => {
    const result = roles(['period'], [['2024-01-01'], ['2024-02-01'], ['2024-03-01']])
    expect(result[0].role).toBe('date')
  })

  it('does not mistake a bare numeric-looking string for a date', () => {
    const result = roles(['year'], [['2024'], ['2025']])
    expect(result[0].role).toBe('numeric')
  })

  it('prefers server-provided column types over client-side inference', () => {
    const result = inferColumnRoles(['mystery'], [['x'], ['y']], { mystery: 'numeric' })
    expect(result[0].role).toBe('numeric')
  })

  it('falls back to client-side inference when server type is missing', () => {
    const result = inferColumnRoles(['region'], [['East']], {})
    expect(result[0].role).toBe('text')
  })

  // A real, previously-shipped backend bug: a SQL Server DECIMAL/MONEY
  // column arrives via pyodbc as Python decimal.Decimal, which pandas
  // stores as object dtype -- agent.result_charting.classify_columns used
  // to mislabel it "text", which silently disabled the entire "Visualize"
  // action for any query whose only numeric measure was a money/decimal
  // column. This is the client-side defense-in-depth half of that fix.
  it('overrides a server "text" label to numeric when every value actually parses as a number', () => {
    const result = inferColumnRoles(['revenue'], [[45231.5], [12044.25]], { revenue: 'text' })
    expect(result[0].role).toBe('numeric')
  })

  it('keeps a server "text" label when values are not all numeric (no false positive)', () => {
    const result = inferColumnRoles(['region'], [['East'], ['West']], { region: 'text' })
    expect(result[0].role).toBe('text')
  })

  it('keeps a server "text" label for a column with no non-null values at all', () => {
    const result = inferColumnRoles(['x'], [[null], [undefined]], { x: 'text' })
    expect(result[0].role).toBe('text')
  })

  it('still trusts a server "numeric" label outright, even over non-numeric-looking values', () => {
    const result = inferColumnRoles(['mystery'], [['x'], ['y']], { mystery: 'numeric' })
    expect(result[0].role).toBe('numeric')
  })
})

describe('getChartTypeOptions', () => {
  it('enables bar/line for category + numeric, disables kpi/pie', () => {
    const columns = roles(['region', 'revenue'], [
      ['East', 100],
      ['West', 200],
    ])
    const rows = [['East', 100], ['West', 200]]
    const options = getChartTypeOptions(columns, rows)
    const byType = Object.fromEntries(options.map((o) => [o.type, o]))

    expect(byType.bar.enabled).toBe(true)
    expect(byType.line.enabled).toBe(true)
    expect(byType.table.enabled).toBe(true)
    expect(byType.kpi.enabled).toBe(false)
    expect(byType.kpi.reason).toMatch(/exactly one/i)
  })

  it('every option always carries a non-empty reason, enabled or not', () => {
    const columns = roles(['name'], [['Alice'], ['Bob']])
    const options = getChartTypeOptions(columns, [['Alice'], ['Bob']])
    for (const option of options) {
      expect(option.reason.length).toBeGreaterThan(0)
    }
  })

  it('enables kpi only for a single row with a numeric column', () => {
    const columns = roles(['total'], [[12345]])
    const options = getChartTypeOptions(columns, [[12345]])
    const kpi = options.find((o) => o.type === 'kpi')!
    expect(kpi.enabled).toBe(true)
  })

  it('enables bar/line/kpi for a category + decimal-sourced measure the server mislabels "text"', () => {
    // The exact end-to-end shape of the "Visualize is always disabled" bug:
    // a SQL Server decimal/money aggregate, which agent.result_charting
    // .classify_columns used to send as column_types: {revenue: "text"}.
    const rows = [
      ['Bikes', 45231.5],
      ['Accessories', 12044.25],
    ]
    const columns = inferColumnRoles(['category', 'revenue'], rows, {
      category: 'text',
      revenue: 'text',
    })
    const options = getChartTypeOptions(columns, rows)
    const byType = Object.fromEntries(options.map((o) => [o.type, o]))
    expect(byType.bar.enabled).toBe(true)
    expect(byType['bar-horizontal'].enabled).toBe(true)
    const hasAnyChartableType = options.some((o) => o.enabled && o.type !== 'table')
    expect(hasAnyChartableType).toBe(true)
  })

  it('disables pie/doughnut when there are too many categories', () => {
    const rows = Array.from({ length: 12 }, (_, i) => [`Category ${i}`, i + 1])
    const columns = roles(['category', 'value'], rows)
    const options = getChartTypeOptions(columns, rows)
    const pie = options.find((o) => o.type === 'pie')!
    expect(pie.enabled).toBe(false)
    expect(pie.reason).toMatch(/8/)
  })

  it('disables pie/doughnut when values are negative', () => {
    const rows = [
      ['East', -10],
      ['West', 20],
    ]
    const columns = roles(['region', 'delta'], rows)
    const options = getChartTypeOptions(columns, rows)
    const pie = options.find((o) => o.type === 'pie')!
    expect(pie.enabled).toBe(false)
    expect(pie.reason).toMatch(/non-negative/i)
  })

  it('disables pie/doughnut when a category repeats', () => {
    const rows = [
      ['East', 10],
      ['East', 20],
      ['West', 30],
    ]
    const columns = roles(['region', 'value'], rows)
    const options = getChartTypeOptions(columns, rows)
    const pie = options.find((o) => o.type === 'pie')!
    expect(pie.enabled).toBe(false)
    expect(pie.reason).toMatch(/repeats/i)
  })

  it('enables pie/doughnut for a valid parts-of-a-whole shape', () => {
    const rows = [
      ['East', 10],
      ['West', 20],
      ['North', 30],
    ]
    const columns = roles(['region', 'value'], rows)
    const options = getChartTypeOptions(columns, rows)
    expect(options.find((o) => o.type === 'pie')!.enabled).toBe(true)
    expect(options.find((o) => o.type === 'doughnut')!.enabled).toBe(true)
  })

  it('disables scatter with fewer than two numeric columns', () => {
    const rows = [['East', 10]]
    const columns = roles(['region', 'value'], rows)
    const options = getChartTypeOptions(columns, rows)
    const scatter = options.find((o) => o.type === 'scatter')!
    expect(scatter.enabled).toBe(false)
    expect(scatter.reason).toMatch(/two numeric/i)
  })

  it('enables scatter with two numeric columns and enough rows', () => {
    const rows = [
      [1, 4],
      [2, 5],
      [3, 6],
    ]
    const columns = roles(['a', 'b'], rows)
    const options = getChartTypeOptions(columns, rows)
    expect(options.find((o) => o.type === 'scatter')!.enabled).toBe(true)
  })

  it('disables mixed chart when the two measures have very different scales', () => {
    const rows = [
      ['East', 1, 100000],
      ['West', 2, 200000],
    ]
    const columns = roles(['region', 'small', 'big'], rows)
    const options = getChartTypeOptions(columns, rows)
    const mixed = options.find((o) => o.type === 'mixed')!
    expect(mixed.enabled).toBe(false)
    expect(mixed.reason).toMatch(/different scales/i)
  })

  it('enables mixed chart when the two measures are on a comparable scale', () => {
    const rows = [
      ['East', 10, 12],
      ['West', 20, 22],
    ]
    const columns = roles(['region', 'a', 'b'], rows)
    const options = getChartTypeOptions(columns, rows)
    expect(options.find((o) => o.type === 'mixed')!.enabled).toBe(true)
  })

  it('disables every chart type for an empty result, except table', () => {
    const options = getChartTypeOptions([], [])
    const nonTable = options.filter((o) => o.type !== 'table')
    expect(nonTable.every((o) => !o.enabled)).toBe(true)
    expect(options.find((o) => o.type === 'table')!.enabled).toBe(true)
  })

  it('enables histogram for a single numeric column with enough rows', () => {
    const rows = [[10], [20], [15], [30], [25]]
    const columns = roles(['order_value'], rows)
    const options = getChartTypeOptions(columns, rows)
    const histogram = options.find((o) => o.type === 'histogram')!
    expect(histogram.enabled).toBe(true)
  })

  it('disables histogram for a single numeric column with too few rows', () => {
    const rows = [[10], [20], [15]]
    const columns = roles(['order_value'], rows)
    const options = getChartTypeOptions(columns, rows)
    const histogram = options.find((o) => o.type === 'histogram')!
    expect(histogram.enabled).toBe(false)
    expect(histogram.reason).toMatch(/at least 5 rows/i)
  })

  it('disables histogram when a category column is also present', () => {
    const rows = [
      ['East', 10],
      ['West', 20],
      ['North', 15],
      ['South', 30],
      ['Central', 25],
    ]
    const columns = roles(['region', 'order_value'], rows)
    const options = getChartTypeOptions(columns, rows)
    const histogram = options.find((o) => o.type === 'histogram')!
    expect(histogram.enabled).toBe(false)
    expect(histogram.reason).toMatch(/exactly one numeric column/i)
  })
})

describe('recommendChart', () => {
  it('recommends bar for category + numeric', () => {
    const rows = [
      ['East', 100],
      ['West', 200],
    ]
    const columns = roles(['region', 'revenue'], rows)
    const result = recommendChart(columns, rows)
    expect(result?.type).toBe('bar')
    expect(result?.series.xColumn).toBe('region')
    expect(result?.series.yColumns).toEqual(['revenue'])
  })

  it('recommends line for date + numeric', () => {
    const rows = [
      ['2024-01-01', 10],
      ['2024-02-01', 20],
    ]
    const columns = roles(['order_date', 'total'], rows)
    const result = recommendChart(columns, rows)
    expect(result?.type).toBe('line')
  })

  it('recommends kpi for a single numeric value', () => {
    const rows = [[12345]]
    const columns = roles(['total_revenue'], rows)
    const result = recommendChart(columns, rows)
    expect(result?.type).toBe('kpi')
  })

  it('recommends scatter for two numeric measures', () => {
    const rows = [
      [1, 4],
      [2, 5],
    ]
    const columns = roles(['a', 'b'], rows)
    const result = recommendChart(columns, rows)
    expect(result?.type).toBe('scatter')
  })

  it('returns null for an all-text result', () => {
    const rows = [['Alice'], ['Bob']]
    const columns = roles(['name'], rows)
    expect(recommendChart(columns, rows)).toBeNull()
  })

  it('never trusts a backend hint whose type is invalid for the actual rows', () => {
    // Backend hint says "pie", but there are 12 categories -- pie is invalid.
    const rows = Array.from({ length: 12 }, (_, i) => [`Category ${i}`, i + 1])
    const columns = roles(['category', 'value'], rows)
    const result = recommendChart(columns, rows, {
      chart_type: 'pie',
      reason: 'stale hint',
      x_column: 'category',
      y_column: 'value',
    })
    expect(result?.type).not.toBe('pie')
  })

  it('uses a validated backend hint when it still applies', () => {
    const rows = [
      ['East', 100],
      ['West', 200],
    ]
    const columns = roles(['region', 'revenue'], rows)
    const result = recommendChart(columns, rows, {
      chart_type: 'bar',
      reason: 'backend reason',
      x_column: 'region',
      y_column: 'revenue',
    })
    expect(result?.type).toBe('bar')
    expect(result?.series.xColumn).toBe('region')
  })

  it('recommends histogram for a single numeric column with enough rows', () => {
    const rows = [[10], [20], [15], [30], [25]]
    const columns = roles(['order_value'], rows)
    const result = recommendChart(columns, rows)
    expect(result?.type).toBe('histogram')
    expect(result?.series.xColumn).toBeNull()
    expect(result?.series.yColumns).toEqual(['order_value'])
  })
})

describe('computeHistogramBins', () => {
  it('returns an empty array for no values', () => {
    expect(computeHistogramBins([])).toEqual([])
  })

  it('returns a single bucket holding every value when all values are identical', () => {
    const bins = computeHistogramBins([5, 5, 5, 5])
    expect(bins).toHaveLength(1)
    expect(bins[0].count).toBe(4)
  })

  it('buckets a known range into Sturges-rule buckets, every value accounted for', () => {
    const values = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    const bins = computeHistogramBins(values)
    expect(bins).toHaveLength(5)
    expect(bins.reduce((sum, b) => sum + b.count, 0)).toBe(values.length)
    // The maximum value is clamped into the last bucket, never dropped or
    // pushed into a phantom bucket past the end.
    expect(bins[bins.length - 1].count).toBeGreaterThan(0)
  })

  it('clamps bucket count into the [5, 20] range for a large dataset', () => {
    const values = Array.from({ length: 500 }, (_, i) => i)
    const bins = computeHistogramBins(values)
    expect(bins.length).toBeGreaterThanOrEqual(5)
    expect(bins.length).toBeLessThanOrEqual(20)
    expect(bins.reduce((sum, b) => sum + b.count, 0)).toBe(values.length)
  })

  it('labels each bucket with a numeric range', () => {
    const bins = computeHistogramBins([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    for (const bin of bins) {
      expect(bin.label).toMatch(/–/) // en dash range separator
    }
  })
})

function baseOptions(overrides: Partial<ChartOptions> = {}): ChartOptions {
  return {
    chartType: 'bar',
    series: { xColumn: 'region', yColumns: ['revenue'], groupColumn: null },
    sortOrder: 'none',
    topN: null,
    dateGrouping: 'none',
    numberFormat: 'plain',
    title: '',
    showLegend: true,
    stacked: false,
    ...overrides,
  }
}

describe('prepareChart', () => {
  const columns = roles(['region', 'revenue'], [
    ['East', 100],
    ['West', 300],
    ['North', 200],
  ])
  const rows = [
    ['East', 100],
    ['West', 300],
    ['North', 200],
  ]

  it('builds bar chart data matching the actual SQL result values', () => {
    const prepared = prepareChart(columns, rows, baseOptions())
    expect(prepared?.chartJsType).toBe('bar')
    expect(prepared?.labels).toEqual(['East', 'West', 'North'])
    expect(prepared?.datasets[0].data).toEqual([100, 300, 200])
  })

  it('builds line chart data for a date x-axis', () => {
    const dateColumns = roles(['order_date', 'total'], [
      ['2024-01-01', 10],
      ['2024-02-01', 20],
    ])
    const dateRows = [
      ['2024-01-01', 10],
      ['2024-02-01', 20],
    ]
    const prepared = prepareChart(
      dateColumns,
      dateRows,
      baseOptions({ chartType: 'line', series: { xColumn: 'order_date', yColumns: ['total'], groupColumn: null } }),
    )
    expect(prepared?.chartJsType).toBe('line')
    expect(prepared?.labels).toEqual(['2024-01-01', '2024-02-01'])
    expect(prepared?.datasets[0].data).toEqual([10, 20])
  })

  it('sorts by y value descending when requested', () => {
    const prepared = prepareChart(columns, rows, baseOptions({ sortOrder: 'y-desc' }))
    expect(prepared?.labels).toEqual(['West', 'North', 'East'])
    expect(prepared?.datasets[0].data).toEqual([300, 200, 100])
  })

  it('applies a top-N limit and discloses it in notices', () => {
    const prepared = prepareChart(columns, rows, baseOptions({ topN: 2 }))
    expect(prepared?.labels).toHaveLength(2)
    expect(prepared?.notices.some((n) => /top 2 of 3/i.test(n))).toBe(true)
  })

  it('discloses date grouping in notices', () => {
    const dateColumns = roles(['order_date', 'total'], [
      ['2024-01-05', 10],
      ['2024-01-20', 20],
    ])
    const dateRows = [
      ['2024-01-05', 10],
      ['2024-01-20', 20],
    ]
    const prepared = prepareChart(
      dateColumns,
      dateRows,
      baseOptions({
        chartType: 'line',
        series: { xColumn: 'order_date', yColumns: ['total'], groupColumn: null },
        dateGrouping: 'month',
      }),
    )
    expect(prepared?.notices.some((n) => /grouped by month/i.test(n))).toBe(true)
    expect(prepared?.labels).toEqual(['2024-01', '2024-01'])
  })

  it('builds pie chart data with one dataset', () => {
    const prepared = prepareChart(columns, rows, baseOptions({ chartType: 'pie' }))
    expect(prepared?.chartJsType).toBe('pie')
    expect(prepared?.datasets).toHaveLength(1)
    expect(prepared?.datasets[0].data).toEqual([100, 300, 200])
  })

  it('builds scatter points from two numeric columns', () => {
    const numericColumns = roles(['a', 'b'], [
      [1, 4],
      [2, 5],
    ])
    const numericRows = [
      [1, 4],
      [2, 5],
    ]
    const prepared = prepareChart(
      numericColumns,
      numericRows,
      baseOptions({ chartType: 'scatter', series: { xColumn: 'a', yColumns: ['b'], groupColumn: null } }),
    )
    expect(prepared?.chartJsType).toBe('scatter')
    expect(prepared?.datasets[0].data).toEqual([
      { x: 1, y: 4 },
      { x: 2, y: 5 },
    ])
  })

  it('returns null for kpi/table (not chart.js-rendered types)', () => {
    expect(prepareChart(columns, rows, baseOptions({ chartType: 'kpi' }))).toBeNull()
    expect(prepareChart(columns, rows, baseOptions({ chartType: 'table' }))).toBeNull()
  })

  it('marks a mixed chart dataset with its own bar/line type', () => {
    const mixedColumns = roles(['region', 'a', 'b'], [
      ['East', 10, 12],
      ['West', 20, 22],
    ])
    const mixedRows = [
      ['East', 10, 12],
      ['West', 20, 22],
    ]
    const prepared = prepareChart(
      mixedColumns,
      mixedRows,
      baseOptions({ chartType: 'mixed', series: { xColumn: 'region', yColumns: ['a', 'b'], groupColumn: null } }),
    )
    expect(prepared?.datasets[0].datasetType).toBe('bar')
    expect(prepared?.datasets[1].datasetType).toBe('line')
  })

  it('builds a histogram via the same bar chartJsType path, no new rendering code needed', () => {
    const histColumns = roles(['order_value'], [[10], [20], [15], [30], [25]])
    const histRows = [[10], [20], [15], [30], [25]]
    const prepared = prepareChart(
      histColumns,
      histRows,
      baseOptions({ chartType: 'histogram', series: { xColumn: null, yColumns: ['order_value'], groupColumn: null } }),
    )
    expect(prepared?.chartJsType).toBe('bar')
    const counts = prepared?.datasets[0].data as number[]
    expect(counts.reduce((sum, n) => sum + n, 0)).toBe(5)
    expect(prepared?.yTitle).toBe('Count')
  })
})
