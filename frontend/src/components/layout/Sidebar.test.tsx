import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { newHistoryEntry, type ConversationSummary } from '@/lib/history'
import type { AskResponse } from '@/lib/types'
import { useChatStore } from '@/store/chatStore'
import { useLocalAuthStore } from '@/store/localAuthStore'
import { Sidebar } from './Sidebar'

// Only useChatSearch's own module is mocked (not the whole identityApi
// surface) -- searchChatHistory is what useChatSearch calls internally,
// and mocking it here (rather than the hook) keeps this test exercising
// the real useChatSearch debounce/stale-response logic, only faking the
// network boundary underneath it.
vi.mock('@/lib/identityApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/identityApi')>()
  return { ...actual, searchChatHistory: vi.fn() }
})
const { searchChatHistory } = await import('@/lib/identityApi')

// Sidebar itself no longer touches @tanstack/react-query -- settings
// (which does, via HistorySettingsSection) moved out entirely to the
// header's UserMenu/SettingsDialog (see UserMenu.test.tsx), so this file
// no longer needs a QueryClientProvider wrapper.
function renderSidebar(props: Parameters<typeof Sidebar>[0] = {}) {
  return render(<Sidebar {...props} />)
}

function makeFinalState(): AskResponse {
  return {
    session_id: 's1',
    conversation_id: 'c1',
    message_id: null,
    status: 'succeeded',
    database: 'default',
    model: 'llama3.1:8b',
    sql: 'SELECT 1',
    result_columns: null,
    result_rows: null,
    row_count: 1,
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
  }
}

function makeConversation(id: string, title: string): ConversationSummary {
  return {
    id,
    title,
    updatedAt: new Date().toISOString(),
    entries: [newHistoryEntry(title, makeFinalState(), 100)],
    messagesLoaded: true,
  }
}

// Sidebar reads directly from the real chatStore singleton (this app has
// no Provider-based store wiring) -- snapshot/restore around each test so
// this file's own state writes never leak into other test files that
// import the same store.
const initialChatState = useChatStore.getState()
const initialLocalAuthState = useLocalAuthStore.getState()

describe('Sidebar', () => {
  beforeEach(() => {
    useChatStore.setState(
      {
        ...initialChatState,
        conversations: {
          c1: makeConversation('c1', 'Revenue last quarter'),
          c2: makeConversation('c2', 'Top products'),
        },
        activeConversationId: 'c1',
        isHistoryHydrated: true,
      },
      true,
    )
    vi.mocked(searchChatHistory).mockReset()
  })

  afterEach(() => {
    useChatStore.setState(initialChatState, true)
    useLocalAuthStore.setState(initialLocalAuthState, true)
  })

  it('renders the New Chat button and every conversation title', () => {
    renderSidebar()
    expect(screen.getByRole('button', { name: /new chat/i })).toBeInTheDocument()
    expect(screen.getByText('Revenue last quarter')).toBeInTheDocument()
    expect(screen.getByText('Top products')).toBeInTheDocument()
  })

  it('starts a new chat and calls onNavigate when New Chat is clicked', async () => {
    let navigated = false
    renderSidebar({ onNavigate: () => (navigated = true) })
    await userEvent.click(screen.getByRole('button', { name: /new chat/i }))
    expect(navigated).toBe(true)
    // startNewChat() resets activeConversationId to a freshly minted id,
    // distinct from the fixture's 'c1'.
    expect(useChatStore.getState().activeConversationId).not.toBe('c1')
  })

  it('filters the visible conversations as the user types in search (client-side, no server history)', async () => {
    renderSidebar()
    const search = screen.getByRole('searchbox')
    await userEvent.type(search, 'Revenue')
    expect(screen.getByText('Revenue last quarter')).toBeInTheDocument()
    expect(screen.queryByText('Top products')).not.toBeInTheDocument()
  })

  it('does not render its own settings, theme, or sign-out controls -- those live only in the header UserMenu', () => {
    renderSidebar()
    expect(screen.queryByRole('button', { name: /^settings$/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /sign out/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('collapsed: shows only the New Chat icon button, hides search and the conversation list', () => {
    renderSidebar({ collapsed: true })
    expect(screen.getByRole('button', { name: /new chat/i })).toBeInTheDocument()
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByText('Revenue last quarter')).not.toBeInTheDocument()
  })

  it('collapsed: New Chat still works', async () => {
    renderSidebar({ collapsed: true })
    await userEvent.click(screen.getByRole('button', { name: /new chat/i }))
    expect(useChatStore.getState().activeConversationId).not.toBe('c1')
  })

  it('New Chat never calls a server API, so double-clicking it cannot create a duplicate conversation', async () => {
    // startNewChat() is a pure local state reset (see chatStore.ts) --
    // the real conversation only gets created server-side by the FIRST
    // /ask call, which is guarded separately by ChatInput's own
    // pendingQuestion-disables-Send lock (see chatStore.test.ts). This
    // test locks in that New Chat itself has no server side effect at
    // all, so no amount of re-clicking it can duplicate anything.
    renderSidebar()
    const newChatButton = screen.getByRole('button', { name: /new chat/i })
    await userEvent.click(newChatButton)
    const idAfterFirstClick = useChatStore.getState().activeConversationId
    await userEvent.click(newChatButton)
    const idAfterSecondClick = useChatStore.getState().activeConversationId

    // Both clicks are real, independent resets (each mints a fresh id) --
    // the point isn't that the id is stable, it's that `conversations`
    // (the map the sidebar list renders from) never grew an extra entry
    // from either click, since neither one is a persistence call.
    expect(Object.keys(useChatStore.getState().conversations)).toEqual(['c1', 'c2'])
    expect(idAfterFirstClick).not.toBe('c1')
    expect(idAfterSecondClick).not.toBe('c1')
  })

  describe('server-side search result selection', () => {
    beforeEach(() => {
      useLocalAuthStore.setState({
        status: 'authenticated',
        user: {
          id: 'u1',
          email: 'ada@example.com',
          username: null,
          display_name: 'Ada Lovelace',
          needs_profile_completion: false,
          status: 'active',
        } as never,
      })
    })

    it('loads the correct conversation (not stale frontend state) when a search result is selected', async () => {
      vi.mocked(searchChatHistory).mockResolvedValue({
        results: [
          {
            conversation_id: 'c2',
            title: 'Top products',
            matched_in: 'message',
            snippet: '...best-selling item was...',
            message_id: 'm-42',
            updated_at: new Date().toISOString(),
          },
        ],
        total: 1,
        limit: 20,
        offset: 0,
        query: 'best-selling',
      })

      renderSidebar()
      await userEvent.type(screen.getByRole('searchbox'), 'best-selling')
      // useChatSearch debounces 300ms before actually calling the API --
      // see that hook's own module docstring.
      await screen.findByText('Top products', {}, { timeout: 2000 })

      await userEvent.click(screen.getByText('Top products'))

      // c2 is already hydrated with messagesLoaded=true in this test's
      // fixture, so loadConversation('c2') resolves synchronously to it
      // -- this asserts the *conversation actually referenced by the
      // clicked search hit* becomes active, not whatever was active
      // before searching (which was 'c1').
      expect(useChatStore.getState().activeConversationId).toBe('c2')
    })

    it('sends the query to the real server search endpoint via searchChatHistory, not a client-side filter', async () => {
      vi.mocked(searchChatHistory).mockResolvedValue({
        results: [],
        total: 0,
        limit: 20,
        offset: 0,
        query: 'quarterly revenue',
      })

      renderSidebar()
      await userEvent.type(screen.getByRole('searchbox'), 'quarterly revenue')

      await vi.waitFor(() => expect(searchChatHistory).toHaveBeenCalledWith('quarterly revenue'), {
        timeout: 2000,
      })
    })
  })
})
