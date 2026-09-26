import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { prepareChart, type ChartOptions, type ColumnInfo } from '@/lib/chartEngine'
import { ResultChart } from './ResultChart'

/** jsdom has no real <canvas> 2D context (`HTMLCanvasElement.getContext`
 * is unimplemented, confirmed empirically -- Chart.js logs "can't acquire
 * context" and no-ops rather than throwing), so these tests verify the
 * render pipeline never crashes and produces the expected DOM shape for
 * every chart type -- NOT actual pixel output. Chart *data* correctness
 * (values match the real SQL result, sorting, top-N, date grouping) is
 * covered at the data layer instead, in chartEngine.test.ts, which needs
 * no canvas at all. */

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

const categoryColumns: ColumnInfo[] = [
  { name: 'region', role: 'text' },
  { name: 'revenue', role: 'numeric' },
]
const categoryRows = [
  ['East', 100],
  ['West', 200],
]

describe('ResultChart', () => {
  it('renders a bar chart without throwing', () => {
    const prepared = prepareChart(categoryColumns, categoryRows, baseOptions())!
    const { container } = render(
      <ResultChart prepared={prepared} chartType="bar" title="" showLegend numberFormat="plain" stacked={false} />,
    )
    expect(container.querySelector('canvas')).not.toBeNull()
  })

  it('renders a line chart for time-series data without throwing', () => {
    const dateColumns: ColumnInfo[] = [
      { name: 'order_date', role: 'date' },
      { name: 'total', role: 'numeric' },
    ]
    const dateRows = [
      ['2024-01-01', 10],
      ['2024-02-01', 20],
    ]
    const prepared = prepareChart(
      dateColumns,
      dateRows,
      baseOptions({ chartType: 'line', series: { xColumn: 'order_date', yColumns: ['total'], groupColumn: null } }),
    )!
    const { container } = render(
      <ResultChart prepared={prepared} chartType="line" title="" showLegend numberFormat="plain" stacked={false} />,
    )
    expect(container.querySelector('canvas')).not.toBeNull()
  })

  it('renders a pie chart without throwing', () => {
    const prepared = prepareChart(categoryColumns, categoryRows, baseOptions({ chartType: 'pie' }))!
    const { container } = render(
      <ResultChart prepared={prepared} chartType="pie" title="" showLegend numberFormat="plain" stacked={false} />,
    )
    expect(container.querySelector('canvas')).not.toBeNull()
  })

  it('renders a scatter chart without throwing', () => {
    const numericColumns: ColumnInfo[] = [
      { name: 'a', role: 'numeric' },
      { name: 'b', role: 'numeric' },
    ]
    const numericRows = [
      [1, 4],
      [2, 5],
    ]
    const prepared = prepareChart(
      numericColumns,
      numericRows,
      baseOptions({ chartType: 'scatter', series: { xColumn: 'a', yColumns: ['b'], groupColumn: null } }),
    )!
    const { container } = render(
      <ResultChart prepared={prepared} chartType="scatter" title="" showLegend numberFormat="plain" stacked={false} />,
    )
    expect(container.querySelector('canvas')).not.toBeNull()
  })

  it('applies an aria-label so the chart has an accessible name', () => {
    const prepared = prepareChart(categoryColumns, categoryRows, baseOptions({ title: 'Revenue by region' }))!
    const { getByRole } = render(
      <ResultChart
        prepared={prepared}
        chartType="bar"
        title="Revenue by region"
        showLegend
        numberFormat="plain"
        stacked={false}
      />,
    )
    expect(getByRole('img', { name: 'Revenue by region' })).toBeTruthy()
  })
})
