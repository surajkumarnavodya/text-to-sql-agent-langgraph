import { beforeEach, describe, expect, it } from 'vitest'
import { newHistoryEntry, withConfirmedResult } from '@/lib/history'
import type { AskResponse } from '@/lib/types'
import { useChatStore } from './chatStore'

const initialState = useChatStore.getState()

function succeededAskResponse(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    session_id: 's1',
    conversation_id: null,
    message_id: null,
    status: 'succeeded',
    database: 'default',
    model: 'llama3.1:8b',
    sql: 'SELECT region, revenue FROM sales',
    result_columns: ['region', 'revenue'],
    result_rows: [
      ['East', 100],
      ['West', 300],
      ['North', 200],
    ],
    row_count: 3,
    retry_count: 0,
    attempt_history: [],
    insight: null,
    cost_notice: null,
    low_confidence_notice: null,
    rejection_reason: null,
    rejection_message: null,
    rate_limit_message: null,
    clarification_message: null,
    failure_explanation: null,
    error_history: [],
    sources_used: ['sql'],
    synthesized_answer: null,
    document_result: null,
    policy_result: null,
    web_result: null,
    generation_result: null,
    media_search_result: null,
    attachment_result: null,
    query_plan: null,
    schema_tables: [],
    followup_classification: null,
    followup_resolved_against: null,
    permission_denied_notice: null,
    analytical_result: null,
    forecast_result: null,
    recommendations: [],
    analytical_intent: null,
    analytical_plan: null,
    governing_metrics: [],
    restricted_field_notice: null,
    ...overrides,
  }
}

/** Seeds queryHistory with one turn that already has a confirmed,
 * chartable SQL result (category + numeric column) -- the state
 * `tryApplyChartTypeFollowup` needs to have anything to switch. */
function seedConfirmedTurn() {
  const finalState = succeededAskResponse()
  const entry = newHistoryEntry('revenue by region', finalState, 500)
  const confirmed = withConfirmedResult(
    entry,
    ['region', 'revenue'],
    [
      ['East', 100],
      ['West', 300],
      ['North', 200],
    ],
    finalState.sql!,
    { region: 'text', revenue: 'numeric' },
    null,
    false,
    120,
  )
  useChatStore.setState({ queryHistory: [confirmed] })
  return confirmed
}

describe('chatStore.tryApplyChartTypeFollowup', () => {
  beforeEach(() => {
    useChatStore.setState(initialState, true)
  })

  it('returns { handled: false } for an ordinary question, leaving queryHistory untouched', () => {
    seedConfirmedTurn()
    const outcome = useChatStore.getState().tryApplyChartTypeFollowup('how many orders were placed last month')
    expect(outcome).toEqual({ handled: false })
    expect(useChatStore.getState().queryHistory[0].chartOptions).toBeNull()
  })

  it('applies a valid chart-type switch directly, without touching confirmedRows/confirmedSql', () => {
    const confirmed = seedConfirmedTurn()
    const outcome = useChatStore.getState().tryApplyChartTypeFollowup('show this as a pie chart')
    expect(outcome).toMatchObject({ handled: true, applied: true })

    const updated = useChatStore.getState().queryHistory[0]
    expect(updated.chartOptions?.chartType).toBe('pie')
    expect(updated.confirmedRows).toEqual(confirmed.confirmedRows)
    expect(updated.confirmedSql).toBe(confirmed.confirmedSql)
  })

  it('is case-insensitive and tolerates a trailing question mark', () => {
    seedConfirmedTurn()
    const outcome = useChatStore.getState().tryApplyChartTypeFollowup('Switch To A Line Chart?')
    expect(outcome).toMatchObject({ handled: true, applied: true })
    expect(useChatStore.getState().queryHistory[0].chartOptions?.chartType).toBe('line')
  })

  it('rejects a chart type that is invalid for the actual result, with a reason, and applies nothing', () => {
    seedConfirmedTurn()
    // A KPI/stat card needs exactly one summary row; this result has 3.
    const outcome = useChatStore.getState().tryApplyChartTypeFollowup('show this as a stat card')
    expect(outcome.handled).toBe(true)
    if (outcome.handled) {
      expect(outcome.applied).toBe(false)
      expect(outcome.message.length).toBeGreaterThan(0)
    }
    expect(useChatStore.getState().queryHistory[0].chartOptions).toBeNull()
  })

  it('reports no chart yet when there is no confirmed result at all', () => {
    useChatStore.setState({ queryHistory: [] })
    const outcome = useChatStore.getState().tryApplyChartTypeFollowup('show this as a pie chart')
    expect(outcome).toMatchObject({ handled: true, applied: false })
  })

  it('targets the most recent chartable turn when several exist', () => {
    const first = seedConfirmedTurn()
    const secondFinal = succeededAskResponse({ sql: 'SELECT month, total FROM sales' })
    const secondEntry = newHistoryEntry('total by month', secondFinal, 400)
    const secondConfirmed = withConfirmedResult(
      secondEntry,
      ['month', 'total'],
      [
        ['Jan', 10],
        ['Feb', 20],
      ],
      secondFinal.sql!,
      { month: 'text', total: 'numeric' },
      null,
      false,
      90,
    )
    useChatStore.setState({ queryHistory: [first, secondConfirmed] })

    const outcome = useChatStore.getState().tryApplyChartTypeFollowup('switch to a bar chart')
    expect(outcome).toMatchObject({ handled: true, applied: true })

    const [unchanged, updated] = useChatStore.getState().queryHistory
    expect(unchanged.chartOptions).toBeNull()
    expect(updated.chartOptions?.chartType).toBe('bar')
  })

  it('preserves an existing chart config (title, number format) across a type-only switch', () => {
    seedConfirmedTurn()
    useChatStore.getState().setChartOptions(useChatStore.getState().queryHistory[0].entryId, {
      chartType: 'bar',
      series: { xColumn: 'region', yColumns: ['revenue'], groupColumn: null },
      sortOrder: 'none',
      topN: null,
      dateGrouping: 'none',
      numberFormat: 'currency',
      title: 'Revenue by region',
      showLegend: true,
      stacked: false,
    })

    const outcome = useChatStore.getState().tryApplyChartTypeFollowup('switch to line')
    expect(outcome).toMatchObject({ handled: true, applied: true })

    const updated = useChatStore.getState().queryHistory[0].chartOptions
    expect(updated?.chartType).toBe('line')
    expect(updated?.numberFormat).toBe('currency')
    expect(updated?.title).toBe('Revenue by region')
  })
})
