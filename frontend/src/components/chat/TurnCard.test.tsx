import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { isSqlResult, newHistoryEntry } from '@/lib/history'
import type { AskResponse } from '@/lib/types'
import { TurnCard } from './TurnCard'

// ResultsTable (rendered once a turn has confirmedColumns/confirmedRows)
// reads schema-table metadata via react-query's useSchemaTables() -- a real
// QueryClientProvider is required for any render path that can reach it,
// same convention ChatInput.test.tsx already uses.
function renderTurnCard(props: React.ComponentProps<typeof TurnCard>) {
  const queryClient = new QueryClient()
  return render(
    <QueryClientProvider client={queryClient}>
      <TurnCard {...props} />
    </QueryClientProvider>,
  )
}

/** Direct regression coverage for the reported "reopening a saved
 * conversation can show ... an empty generated-SQL editor with Confirm and
 * Run" bug -- traced to `frontend/src/lib/history.ts` reconstructing every
 * reloaded turn with a hardcoded `sources_used: []`, which this component's
 * own `isSqlResult` check (correctly, in isolation) treats as "this is a
 * SQL-path answer." See `history.test.ts` for the reconstruction-layer
 * fix's own tests; this file covers what the user actually sees rendered. */

function makeAskResponse(overrides: Partial<AskResponse> = {}): AskResponse {
  return {
    session_id: 's1',
    conversation_id: 'c1',
    message_id: 'm1',
    status: 'succeeded',
    database: 'default',
    model: 'llama3.1:8b',
    sql: null,
    result_columns: null,
    result_rows: null,
    row_count: null,
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
    sources_used: [],
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

describe('isSqlResult', () => {
  it('treats an empty sources_used as the SQL path (this app-wide "router off" convention)', () => {
    expect(isSqlResult([])).toBe(true)
  })

  it('treats a non-empty sources_used without "sql" as a non-SQL answer', () => {
    expect(isSqlResult(['web'])).toBe(false)
    expect(isSqlResult(['documents', 'policy'])).toBe(false)
  })

  it('treats a sources_used including "sql" as the SQL path even when mixed with other sources', () => {
    expect(isSqlResult(['sql', 'web'])).toBe(true)
  })
})

describe('TurnCard -- SQL editor / Confirm and Run gating', () => {
  it('shows the SQL editor and Confirm and Run for a genuine SQL-path turn with generated SQL', () => {
    const entry = newHistoryEntry(
      'How many orders?',
      makeAskResponse({ sql: 'SELECT COUNT(*) FROM orders', sources_used: [] }),
      100,
    )
    renderTurnCard({ entry, isMultiDb: false })
    expect(screen.getByRole('button', { name: /confirm and run/i })).toBeInTheDocument()
  })

  it(
    'never shows the SQL editor or Confirm and Run for a reloaded web-only answer, ' +
      'even though sources_used happening to be empty would otherwise look like the SQL path',
    () => {
      const entry = newHistoryEntry(
        'Why is the sky blue?',
        makeAskResponse({
          sources_used: ['web'],
          web_result: { answer: 'Rayleigh scattering.', citations: [], status: 'succeeded' },
        }),
        100,
      )
      renderTurnCard({ entry, isMultiDb: false })
      expect(screen.queryByRole('button', { name: /confirm and run/i })).not.toBeInTheDocument()
      expect(screen.getByText('Rayleigh scattering.')).toBeInTheDocument()
    },
  )

  it(
    'never shows an empty SQL editor for a reconstructed entry with no SQL at all, ' +
      'even when sources_used is empty (the actual reported bug)',
    () => {
      const entry = newHistoryEntry('Some question', makeAskResponse({ sql: null, sources_used: [] }), 0)
      renderTurnCard({ entry, isMultiDb: false })
      expect(screen.queryByRole('button', { name: /confirm and run/i })).not.toBeInTheDocument()
    },
  )

  it('shows a "no recorded answer" notice, and no SQL editor, for a pending/orphaned reconstructed entry', () => {
    const entry = newHistoryEntry('Orphaned question', makeAskResponse({ status: 'pending' }), 0)
    renderTurnCard({ entry, isMultiDb: false })
    expect(screen.getByText('This message has no recorded answer.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /confirm and run/i })).not.toBeInTheDocument()
  })

  it('restores a previously confirmed result without requiring another click of Confirm and Run', () => {
    const base = newHistoryEntry(
      'How many orders?',
      makeAskResponse({ sql: 'SELECT COUNT(*) FROM orders', sources_used: [], insight: '42 orders.' }),
      100,
    )
    const entry = {
      ...base,
      confirmedColumns: ['cnt'],
      confirmedRows: [[42]],
      confirmedSql: 'SELECT COUNT(*) FROM orders',
    }
    renderTurnCard({ entry, isMultiDb: false })
    expect(screen.getByText('42 orders.')).toBeInTheDocument()
  })
})
