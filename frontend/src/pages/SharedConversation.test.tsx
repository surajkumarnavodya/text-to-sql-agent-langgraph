import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as shareApi from '@/lib/shareApi'
import type { SharedConversation as SharedConversationData } from '@/lib/types'
import { SharedConversation } from './SharedConversation'

const { FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    status: number
    constructor(status: number) {
      super('denied')
      this.status = status
    }
  }
  return { FakeApiError }
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, ApiError: FakeApiError }
})
vi.mock('@/lib/shareApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/shareApi')>()
  return { ...actual, getSharedConversation: vi.fn() }
})
vi.mock('@/store/localAuthStore', () => ({
  useLocalAuthStore: { getState: () => ({ initialize: vi.fn().mockResolvedValue(undefined) }) },
}))

function renderAt(ref: string) {
  return render(
    <MemoryRouter initialEntries={[`/shared/${ref}`]}>
      <Routes>
        <Route path="/shared/:ref" element={<SharedConversation />} />
      </Routes>
    </MemoryRouter>,
  )
}

function makeConversation(overrides: Partial<SharedConversationData> = {}): SharedConversationData {
  return {
    conversation_title: 'Revenue last quarter',
    feature_type: 'text_to_sql',
    snapshot_captured_at: '2026-01-01T00:00:00Z',
    viewer_role: 'public_link',
    turns: [
      {
        sequence_number: 1,
        role: 'user',
        content: 'How many orders were there?',
        created_at: '2026-01-01T00:00:00Z',
        sources_used: [],
        database: null,
        model: null,
        sql: null,
        row_count: null,
        insight: null,
        synthesized_answer: null,
        document_result: null,
        policy_result: null,
        web_result: null,
        generation_result: null,
        media_search_result: null,
        attachment_refs: [],
        result_snapshot: null,
      },
      {
        sequence_number: 2,
        role: 'assistant',
        content: 'There were 42 orders.',
        created_at: '2026-01-01T00:00:01Z',
        sources_used: ['sql'],
        database: 'default',
        model: 'llama3.1:8b',
        sql: 'SELECT COUNT(*) FROM orders',
        row_count: 1,
        insight: null,
        synthesized_answer: null,
        document_result: null,
        policy_result: null,
        web_result: null,
        generation_result: null,
        media_search_result: null,
        attachment_refs: [],
        result_snapshot: null,
      },
    ],
    ...overrides,
  }
}

describe('SharedConversation', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders every turn from the server projection, including the SQL as read-only text', async () => {
    vi.mocked(shareApi.getSharedConversation).mockResolvedValue(makeConversation())
    renderAt('share-1')

    await waitFor(() => expect(screen.getByText('How many orders were there?')).toBeInTheDocument())
    expect(screen.getByText('There were 42 orders.')).toBeInTheDocument()
    expect(screen.getByText('SELECT COUNT(*) FROM orders')).toBeInTheDocument()
  })

  it('never renders a "Confirm and Run" button or any SQL-execution affordance', async () => {
    vi.mocked(shareApi.getSharedConversation).mockResolvedValue(makeConversation())
    renderAt('share-1')

    await waitFor(() => screen.getByText('There were 42 orders.'))
    expect(screen.queryByRole('button', { name: /confirm and run/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument() // no composer at all
  })

  it('shows a generic unavailable message on a 404 -- never a raw error or [object Object]', async () => {
    vi.mocked(shareApi.getSharedConversation).mockRejectedValue(new FakeApiError(404))
    renderAt('unknown-ref')

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).not.toContain('[object Object]')
    expect(text.toLowerCase()).toContain('unavailable')
  })

  it('shows a distinguishable rate-limited message on a 429', async () => {
    vi.mocked(shareApi.getSharedConversation).mockRejectedValue(new FakeApiError(429))
    renderAt('share-1')

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(screen.getByText(/too many requests/i)).toBeInTheDocument()
  })

  it('renders the snapshot timestamp from the server response', async () => {
    vi.mocked(shareApi.getSharedConversation).mockResolvedValue(makeConversation())
    renderAt('share-1')

    await waitFor(() => screen.getByText('Revenue last quarter'))
    expect(screen.getByText(/snapshot captured/i)).toBeInTheDocument()
  })
})
