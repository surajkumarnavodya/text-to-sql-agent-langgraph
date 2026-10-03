import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { AnalyticalIntent } from '@/lib/types'
import {
  actionRec,
  askState,
  forecastOk,
  forecastRejected,
  nextQuestionRec,
  rankingResult,
  timeSeriesResult,
} from '@/test/analyticsFixtures'
import { AnalyticsSummary } from './AnalyticsSummary'

function intent(name: AnalyticalIntent['intent'], overrides: Partial<AnalyticalIntent> = {}): AnalyticalIntent {
  return {
    intent: name,
    confidence: 0.9,
    metric_candidates: [],
    dimensions: [],
    time_requirement: null,
    comparison: null,
    filters: [],
    expected_result_shape: null,
    ambiguity_flags: [],
    truth_level: 'ai_inference',
    ...overrides,
  }
}

describe('AnalyticsSummary -- real analytics data', () => {
  it('shows headline KPIs, a trend with its change, flagged anomalies, and the engine disclosures', () => {
    render(
      <AnalyticsSummary
        state={askState({ analytical_result: timeSeriesResult, analytical_intent: intent('trend') })}
        cacheStatus={null}
      />,
    )

    const kpis = screen.getByRole('list', { name: 'Key figures' })
    expect(kpis).toHaveTextContent('130')
    expect(kpis).toHaveTextContent('▲ 30.0%')

    const trend = screen.getByRole('region', { name: 'Trend of sales' })
    expect(trend).toHaveTextContent('Over 4 periods it rose (+30.0% overall).')
    expect(trend).toHaveTextContent('Missing periods in the series: 2020.')
    expect(screen.getByRole('img', { name: 'line chart' })).toBeInTheDocument()

    expect(screen.getByRole('region', { name: 'Anomalies (1 flagged)' })).toHaveTextContent(
      'Period-over-period change: deviation -25 against a threshold of 20',
    )
    expect(screen.getByText('Skipped: seasonal: no prior occurrence at lag 12')).toBeInTheDocument()
  })

  it('leads a distribution question with the share view (doughnut), not the bar ranking', () => {
    render(
      <AnalyticsSummary
        state={askState({ analytical_result: rankingResult, analytical_intent: intent('distribution') })}
        cacheStatus={null}
      />,
    )

    expect(screen.getByRole('region', { name: 'Distribution of revenue by category' })).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'doughnut chart' })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'bar-horizontal chart' })).not.toBeInTheDocument()
  })

  it('shows a ranking as bars for a ranking question, with the top share in each row', () => {
    render(
      <AnalyticsSummary
        state={askState({ analytical_result: rankingResult, analytical_intent: intent('ranking') })}
        cacheStatus={null}
      />,
    )

    expect(screen.getByRole('img', { name: 'bar-horizontal chart' })).toBeInTheDocument()
    expect(screen.getByText('1. Bikes')).toBeInTheDocument()
    expect(screen.getByText(/500 · 50\.0%/)).toBeInTheDocument()
  })

  it('makes each ranking row a drill-down that submits a templated follow-up question', () => {
    const onAsk = vi.fn()
    render(
      <AnalyticsSummary
        state={askState({ analytical_result: rankingResult, analytical_intent: intent('ranking') })}
        cacheStatus={null}
        onAsk={onAsk}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '1. Bikes' }))

    expect(onAsk).toHaveBeenCalledWith('Show the details behind Bikes for revenue')
  })

  it('renders ranking rows as plain text, not buttons, when no drill-down handler is provided', () => {
    render(
      <AnalyticsSummary
        state={askState({ analytical_result: rankingResult, analytical_intent: intent('ranking') })}
        cacheStatus={null}
      />,
    )

    expect(screen.queryByRole('button', { name: '1. Bikes' })).not.toBeInTheDocument()
    expect(screen.getByText('1. Bikes')).toBeInTheDocument()
  })

  it('submits a next-question recommendation as a one-click related question', () => {
    const onAsk = vi.fn()
    render(
      <AnalyticsSummary
        state={askState({ recommendations: [nextQuestionRec] })}
        cacheStatus={null}
        onAsk={onAsk}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'What drove the 2023 dip in sales?' }))

    expect(onAsk).toHaveBeenCalledWith('What drove the 2023 dip in sales?')
  })

  it('labels every recommendation as an AI estimate and shows each piece of evidence with its own truth level', () => {
    render(<AnalyticsSummary state={askState({ recommendations: [actionRec] })} cacheStatus={null} />)

    const evidence = screen.getByRole('region', { name: 'Recommendations and evidence' })
    expect(evidence).toHaveTextContent('AI estimate')
    expect(evidence).toHaveTextContent('Suggested action: Review dependence on a single category')
    expect(evidence).toHaveTextContent('Confidence 82%')
    expect(evidence).toHaveTextContent('Bikes share is 50.0% of total revenue')
    expect(evidence.querySelector('[data-truth-level="database_fact"]')).not.toBeNull()
  })

  it('shows governed metric definitions as approved, confirmed business truth', () => {
    render(
      <AnalyticsSummary
        state={askState({
          governing_metrics: [
            {
              business_name: 'Revenue',
              approved_expression: 'SUM(sales.amount)',
              aggregation: 'sum',
              text: 'Revenue is the sum of sales amounts.',
            },
          ],
        })}
        cacheStatus={null}
      />,
    )

    const approved = screen.getByRole('region', { name: 'Approved definitions used' })
    expect(approved).toHaveTextContent('Approved definition')
    expect(approved).toHaveTextContent('SUM(sales.amount)')
    expect(approved).toHaveTextContent('Revenue is the sum of sales amounts.')
  })

  it('shows a rejected forecast as an insufficient-evidence state with its reasons, never an empty chart', () => {
    render(
      <AnalyticsSummary
        state={askState({ analytical_result: timeSeriesResult, forecast_result: forecastRejected })}
        cacheStatus={null}
      />,
    )

    const forecast = screen.getByRole('region', { name: 'Forecast' })
    expect(forecast).toHaveTextContent('A forecast could not be produced for this series.')
    expect(forecast).toHaveTextContent('fewer than 4 historical points')
    expect(forecast.querySelector('canvas')).toBeNull()
  })

  it('shows a successful forecast as an AI estimate with its range, model, backtest and limitations', () => {
    render(
      <AnalyticsSummary
        state={askState({ analytical_result: timeSeriesResult, forecast_result: forecastOk })}
        cacheStatus={null}
      />,
    )

    const forecast = screen.getByRole('region', { name: 'Forecast · next 2 periods' })
    expect(forecast).toHaveTextContent('AI estimate')
    expect(forecast).toHaveTextContent('Estimate, not a confirmed fact.')
    expect(forecast).toHaveTextContent('120 – 160')
    expect(forecast).toHaveTextContent('Model linear_trend trained on 4 periods')
    expect(forecast).toHaveTextContent('MAPE 4.2%')
    expect(forecast).toHaveTextContent('assumes the historical pattern continues unchanged')
  })

  it('says plainly when a time series was checked and nothing was flagged', () => {
    const noAnomalies = {
      ...timeSeriesResult,
      findings: timeSeriesResult.findings.filter((finding) => finding.kind !== 'anomaly'),
    }
    render(<AnalyticsSummary state={askState({ analytical_result: noAnomalies })} cacheStatus={null} />)

    expect(screen.getByText('No unusual points were flagged in this series.')).toBeInTheDocument()
  })

  it('shows the validated query scope and any classifier ambiguity, read-only, in a collapsed section', () => {
    render(
      <AnalyticsSummary
        state={askState({
          analytical_result: rankingResult,
          analytical_intent: intent('ranking', {
            ambiguity_flags: ["'best selling' could mean highest revenue or highest unit count"],
          }),
          analytical_plan: {
            metrics: [{ name: 'revenue', table: 'sales', column: 'amount', aggregation: 'sum', governed_metric_key: null }],
            dimensions: [{ name: 'category', table: 'products', column: 'category' }],
            filters: [{ table: 'sales', column: 'region', operator: '=', value: 'West' }],
            time_range: { table: 'sales', column: 'order_date', description: 'last 6 months' },
            grain: 'month',
            comparison: null,
            ranking: { order_by: 'revenue', direction: 'desc', top_n: 3, per_group: [] },
            sort: [],
            limit: null,
            required_operations: [],
            truth_level: 'ai_inference',
          },
        })}
        cacheStatus={null}
      />,
    )

    const scope = screen.getByText('Query scope').closest('details')
    expect(scope).not.toBeNull()
    expect(scope).toHaveTextContent('revenue (sum)')
    expect(scope).toHaveTextContent('Broken down by')
    expect(scope).toHaveTextContent('region = West')
    expect(scope).toHaveTextContent('last 6 months')
    expect(scope).toHaveTextContent('top 3 by revenue (desc)')
    expect(scope).toHaveTextContent("'best selling' could mean highest revenue or highest unit count")
  })
})

describe('AnalyticsSummary -- honest empty, error and freshness states', () => {
  it('says so when the query matched no rows, and renders no analysis panels', () => {
    render(
      <AnalyticsSummary
        state={askState({
          row_count: 0,
          analytical_result: { row_count: 0, findings: [], shape: 'empty', engine_version: '1.0.0', insufficient_data_reasons: [] },
        })}
        cacheStatus={null}
      />,
    )

    expect(screen.getByText('No rows matched this question, so there is nothing to analyse.')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Key figures' })).not.toBeInTheDocument()
  })

  it('says plainly when a result has no statistical breakdown, instead of rendering an empty card', () => {
    render(<AnalyticsSummary state={askState({ row_count: 3 })} cacheStatus={null} />)

    expect(screen.getByText('No statistical breakdown is available for this kind of result.')).toBeInTheDocument()
  })

  it('tells the user a cache-served result is cached, and a fresh one is live', () => {
    const { unmount } = render(
      <AnalyticsSummary state={askState({ analytical_result: rankingResult })} cacheStatus="hit" />,
    )
    expect(screen.getByText('Served from a recent cached result')).toBeInTheDocument()
    unmount()

    render(<AnalyticsSummary state={askState({ analytical_result: rankingResult })} cacheStatus="miss" />)
    expect(screen.getByText('Live query result')).toBeInTheDocument()
    expect(screen.queryByText('Served from a recent cached result')).not.toBeInTheDocument()
  })

  it('discloses a row-capped ranking instead of presenting it as the full set', () => {
    const truncated = {
      ...rankingResult,
      findings: [{ ...rankingResult.findings[0], ranking: { ...rankingResult.findings[0].ranking!, truncated: true } }],
    }
    render(<AnalyticsSummary state={askState({ analytical_result: truncated })} cacheStatus={null} />)

    expect(screen.getByText(/capped by the row limit/)).toBeInTheDocument()
    expect(screen.queryByText(/^Total revenue$/)).not.toBeInTheDocument()
  })
})
