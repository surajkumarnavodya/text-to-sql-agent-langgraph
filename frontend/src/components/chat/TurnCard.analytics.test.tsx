import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { newHistoryEntry, withConfirmedResult } from '@/lib/history'
import { askState, rankingResult } from '@/test/analyticsFixtures'
import { TurnCard } from './TurnCard'

function renderTurn(entry: ReturnType<typeof newHistoryEntry>) {
  const queryClient = new QueryClient()
  return render(
    <QueryClientProvider client={queryClient}>
      <TurnCard entry={entry} isMultiDb={false} />
    </QueryClientProvider>,
  )
}

const SQL = 'SELECT category, SUM(revenue) FROM sales GROUP BY category'

describe('TurnCard -- analysis section (Prompt 30)', () => {
  it('shows the analysis only after the user confirms and runs the SQL, like the insight it sits beside', () => {
    const entry = newHistoryEntry(
      'Revenue by category',
      askState({ sql: SQL, analytical_result: rankingResult, analytical_intent: null }),
      100,
    )
    renderTurn(entry)

    expect(screen.queryByRole('region', { name: 'Analysis' })).not.toBeInTheDocument()
  })

  it('renders the analysis, with its freshness, once a result is confirmed', async () => {
    const base = newHistoryEntry(
      'Revenue by category',
      askState({ sql: SQL, analytical_result: rankingResult }),
      100,
    )
    const confirmed = withConfirmedResult(
      base,
      ['category', 'revenue'],
      [
        ['Bikes', 500],
        ['Clothing', 300],
        ['Accessories', 200],
      ],
      SQL,
      { category: 'text', revenue: 'numeric' },
      null,
      false,
      25,
      'hit',
    )
    renderTurn(confirmed)

    // The analysis section is lazy-loaded (see TurnCard.tsx), so it resolves after mount.
    const analysis = await screen.findByRole('region', { name: 'Analysis' })
    expect(analysis).toHaveTextContent('Served from a recent cached result')
    expect(await screen.findByRole('list', { name: 'Key figures' })).toBeInTheDocument()
  })

  it('does not show the analysis when the user has edited the SQL away from what produced it', () => {
    const base = newHistoryEntry(
      'Revenue by category',
      askState({ sql: SQL, analytical_result: rankingResult }),
      100,
    )
    const confirmed = {
      ...withConfirmedResult(
        base,
        ['category', 'revenue'],
        [['Bikes', 500]],
        SQL,
        { category: 'text', revenue: 'numeric' },
        null,
        false,
        25,
        null,
      ),
      editableSql: 'SELECT 1',
    }
    renderTurn(confirmed)

    expect(screen.queryByRole('region', { name: 'Analysis' })).not.toBeInTheDocument()
  })

  it('shows a plain-language field-permission notice for a failed answer blocked by a restricted column', () => {
    const entry = newHistoryEntry(
      'Show customer SSNs',
      askState({
        status: 'failed',
        sql: 'SELECT ssn FROM customers',
        failure_explanation: 'Agent could not produce a working query.',
        restricted_field_notice:
          'One or more fields this question needs are access-restricted for your role, so the answer could not be produced. Ask an administrator if you need access.',
      }),
      100,
    )
    renderTurn(entry)

    expect(screen.getByText(/access-restricted for your role/)).toBeInTheDocument()
  })

  it('does not show a field-permission notice for an ordinary failure', () => {
    const entry = newHistoryEntry(
      'Something broken',
      askState({
        status: 'failed',
        sql: 'SELECT broken',
        failure_explanation: 'Agent could not produce a working query.',
        restricted_field_notice: null,
      }),
      100,
    )
    renderTurn(entry)

    expect(screen.queryByText(/access-restricted/)).not.toBeInTheDocument()
  })
})
