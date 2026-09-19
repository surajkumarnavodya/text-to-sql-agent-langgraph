import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { newHistoryEntry, type ConversationSummary } from '@/lib/history'
import type { AskResponse } from '@/lib/types'
import { ConversationListItem } from './ConversationListItem'

function makeConversation(overrides: Partial<ConversationSummary> = {}): ConversationSummary {
  const finalState: AskResponse = {
    session_id: 's1',
    conversation_id: 'c1',
    status: 'succeeded',
    database: 'default',
    sql: 'SELECT 1',
    result_columns: ['n'],
    result_rows: [[1]],
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
    query_plan: null,
    schema_tables: [],
    followup_classification: null,
    followup_resolved_against: null,
  }
  return {
    id: 'c1',
    title: 'Revenue last quarter',
    updatedAt: new Date().toISOString(),
    entries: [newHistoryEntry('What was revenue last quarter?', finalState, 500)],
    messagesLoaded: true,
    ...overrides,
  }
}

function noop() {}

describe('ConversationListItem', () => {
  it('renders the conversation title and calls onSelect when clicked', async () => {
    const onSelect = vi.fn()
    render(
      <ConversationListItem
        conversation={makeConversation()}
        isActive={false}
        isLoading={false}
        isRenaming={false}
        renameValue=""
        onRenameValueChange={noop}
        onSelect={onSelect}
        onStartRename={noop}
        onCommitRename={noop}
        onCancelRename={noop}
        onDelete={noop}
      />,
    )
    await userEvent.click(screen.getByText('Revenue last quarter'))
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('shows a loading label instead of the timestamp when active and loading', () => {
    render(
      <ConversationListItem
        conversation={makeConversation()}
        isActive={true}
        isLoading={true}
        isRenaming={false}
        renameValue=""
        onRenameValueChange={noop}
        onSelect={noop}
        onStartRename={noop}
        onCommitRename={noop}
        onCancelRename={noop}
        onDelete={noop}
      />,
    )
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('renders a rename input and commits on Enter', async () => {
    const onCommitRename = vi.fn()
    const onRenameValueChange = vi.fn()
    render(
      <ConversationListItem
        conversation={makeConversation()}
        isActive={false}
        isLoading={false}
        isRenaming={true}
        renameValue="New title"
        onRenameValueChange={onRenameValueChange}
        onSelect={noop}
        onStartRename={noop}
        onCommitRename={onCommitRename}
        onCancelRename={noop}
        onDelete={noop}
      />,
    )
    const input = screen.getByDisplayValue('New title')
    await userEvent.type(input, '{Enter}')
    expect(onCommitRename).toHaveBeenCalledTimes(1)
  })

  it('cancels rename on Escape', async () => {
    const onCancelRename = vi.fn()
    render(
      <ConversationListItem
        conversation={makeConversation()}
        isActive={false}
        isLoading={false}
        isRenaming={true}
        renameValue="New title"
        onRenameValueChange={noop}
        onSelect={noop}
        onStartRename={noop}
        onCommitRename={noop}
        onCancelRename={onCancelRename}
        onDelete={noop}
      />,
    )
    await userEvent.type(screen.getByDisplayValue('New title'), '{Escape}')
    expect(onCancelRename).toHaveBeenCalledTimes(1)
  })
})
