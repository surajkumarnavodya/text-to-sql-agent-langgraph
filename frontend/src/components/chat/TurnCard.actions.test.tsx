import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ToastProvider } from '@/components/ui/toast'
import { newHistoryEntry } from '@/lib/history'
import { askState } from '@/test/analyticsFixtures'
import { TurnCard } from './TurnCard'

function renderTurn(entry: ReturnType<typeof newHistoryEntry>) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ToastProvider>
        <TurnCard entry={entry} isMultiDb={false} />
      </ToastProvider>
    </QueryClientProvider>,
  )
}

/** Regression coverage for the answer action row: Read aloud is added beside the
 * existing actions, and none of them are displaced or renamed. */
describe('TurnCard answer actions', () => {
  it('renders Copy, Download, and Read aloud together, in that order, for a completed answer', () => {
    const entry = newHistoryEntry(
      'Why is the sky blue?',
      askState({
        sources_used: ['web'],
        web_result: { answer: 'Rayleigh scattering.', citations: [], status: 'succeeded' },
      }),
      100,
    )
    renderTurn(entry)

    const copy = screen.getByRole('button', { name: 'Copy answer' })
    const download = screen.getByRole('button', { name: 'Download answer' })
    const readAloud = screen.getByRole('button', { name: 'Read aloud' })

    expect(copy).toBeInTheDocument()
    expect(download).toBeInTheDocument()
    expect(readAloud).toBeInTheDocument()
    expect(copy.compareDocumentPosition(readAloud) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(download.compareDocumentPosition(readAloud) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('renders no answer action row when there is no answer text', () => {
    const entry = newHistoryEntry('Anything?', askState({ sources_used: ['web'], sql: null }), 100)
    renderTurn(entry)

    expect(screen.queryByRole('button', { name: 'Read aloud' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy answer' })).not.toBeInTheDocument()
  })
})
