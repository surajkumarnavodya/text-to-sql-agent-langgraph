import { describe, expect, it } from 'vitest'
import {
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
})
