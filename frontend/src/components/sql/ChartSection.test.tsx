import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ChartSection } from './ChartSection'

const categoryColumns = ['region', 'revenue']
const categoryRows = [
  ['East', 100],
  ['West', 300],
  ['North', 200],
]
const categoryColumnTypes = { region: 'text', revenue: 'numeric' }

function renderSection(overrides: Partial<Parameters<typeof ChartSection>[0]> = {}) {
  const onChartOptionsChange = vi.fn()
  const utils = render(
    <ChartSection
      columns={categoryColumns}
      rows={categoryRows}
      columnTypes={categoryColumnTypes}
      chartRecommendation={null}
      truncated={false}
      chartOptions={null}
      onChartOptionsChange={onChartOptionsChange}
      {...overrides}
    />,
  )
  return { ...utils, onChartOptionsChange }
}

describe('ChartSection', () => {
  it('does not show a chart by default -- only an opt-in Visualize action', () => {
    renderSection()
    expect(screen.getByRole('button', { name: /visualize/i })).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
  })

  it('renders nothing at all for an empty result', () => {
    const { container } = renderSection({ rows: [] })
    expect(container).toBeEmptyDOMElement()
  })

  it('disables the Visualize action with an explanation for an unchartable shape', () => {
    renderSection({
      columns: ['name'],
      rows: [['Alice'], ['Bob']],
      columnTypes: { name: 'text' },
    })
    const button = screen.getByRole('button', { name: /visualize/i })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', expect.stringMatching(/can't be visualized/i))
  })

  it('clicking Visualize opens the chart type picker with a live preview', async () => {
    renderSection()
    await userEvent.click(screen.getByRole('button', { name: /visualize/i }))
    expect(screen.getByRole('radiogroup')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /generate chart/i })).toBeInTheDocument()
    // A live preview renders immediately, before "Generate chart" is clicked.
    expect(screen.getByRole('img')).toBeInTheDocument()
  })

  it('enables suitable chart types and disables unsuitable ones with a reason', async () => {
    renderSection()
    await userEvent.click(screen.getByRole('button', { name: /visualize/i }))
    const picker = screen.getByRole('radiogroup')
    const barOption = within(picker).getByRole('radio', { name: /^bar$/i })
    const kpiOption = within(picker).getByRole('radio', { name: /stat card/i })
    expect(barOption).toBeEnabled()
    expect(kpiOption).toBeDisabled()
    expect(kpiOption).toHaveAttribute('title', expect.stringMatching(/exactly one/i))
  })

  it('lets the user switch from bar to line where valid', async () => {
    // Date + numeric so both bar (via date-as-category) and line validate.
    renderSection({
      columns: ['order_date', 'total'],
      rows: [
        ['2024-01-01', 10],
        ['2024-02-01', 20],
      ],
      columnTypes: { order_date: 'date', total: 'numeric' },
    })
    await userEvent.click(screen.getByRole('button', { name: /visualize/i }))
    const picker = screen.getByRole('radiogroup')
    const lineOption = within(picker).getByRole('radio', { name: /^line$/i })
    expect(lineOption).toBeEnabled()
    await userEvent.click(lineOption)
    expect(lineOption).toHaveAttribute('aria-checked', 'true')
  })

  it('commits the chart only after "Generate chart" is clicked', async () => {
    const { onChartOptionsChange } = renderSection()
    await userEvent.click(screen.getByRole('button', { name: /visualize/i }))
    expect(onChartOptionsChange).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: /generate chart/i }))
    expect(onChartOptionsChange).toHaveBeenCalledTimes(1)
    expect(onChartOptionsChange.mock.calls[0][0]).toMatchObject({ chartType: 'bar' })
  })

  it('cancelling the picker never calls onChartOptionsChange', async () => {
    const { onChartOptionsChange } = renderSection()
    await userEvent.click(screen.getByRole('button', { name: /visualize/i }))
    await userEvent.click(screen.getByRole('button', { name: /^cancel$/i }))
    expect(onChartOptionsChange).not.toHaveBeenCalled()
    // Back to the closed state -- the Visualize button again, no chart.
    expect(screen.getByRole('button', { name: /visualize/i })).toBeInTheDocument()
  })

  it('shows Customize and Remove chart once a chart exists, and removing it clears it without touching the table', async () => {
    const chartOptions = {
      chartType: 'bar' as const,
      series: { xColumn: 'region', yColumns: ['revenue'], groupColumn: null },
      sortOrder: 'none' as const,
      topN: null,
      dateGrouping: 'none' as const,
      numberFormat: 'plain' as const,
      title: '',
      showLegend: true,
      stacked: false,
    }
    const { onChartOptionsChange } = renderSection({ chartOptions })
    expect(screen.getByRole('img')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /customize/i })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /remove chart/i }))
    expect(onChartOptionsChange).toHaveBeenCalledWith(null)
  })

  it('shows a truncation warning when the result was truncated', () => {
    const chartOptions = {
      chartType: 'bar' as const,
      series: { xColumn: 'region', yColumns: ['revenue'], groupColumn: null },
      sortOrder: 'none' as const,
      topN: null,
      dateGrouping: 'none' as const,
      numberFormat: 'plain' as const,
      title: '',
      showLegend: true,
      stacked: false,
    }
    renderSection({ chartOptions, truncated: true })
    expect(screen.getByText(/only the returned rows/i)).toBeInTheDocument()
  })

  it('reopening via Customize is keyboard-operable (tab + enter)', async () => {
    const chartOptions = {
      chartType: 'bar' as const,
      series: { xColumn: 'region', yColumns: ['revenue'], groupColumn: null },
      sortOrder: 'none' as const,
      topN: null,
      dateGrouping: 'none' as const,
      numberFormat: 'plain' as const,
      title: '',
      showLegend: true,
      stacked: false,
    }
    renderSection({ chartOptions })
    const customizeButton = screen.getByRole('button', { name: /customize/i })
    customizeButton.focus()
    expect(customizeButton).toHaveFocus()
    await userEvent.keyboard('{Enter}')
    expect(screen.getByRole('radiogroup')).toBeInTheDocument()
  })
})
