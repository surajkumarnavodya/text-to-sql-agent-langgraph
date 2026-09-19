import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { SearchHit } from '@/lib/types'
import { ConversationSearchResults } from './ConversationSearchResults'

const hit: SearchHit = {
  conversation_id: 'c1',
  message_id: 'm1',
  title: 'Revenue last quarter',
  snippet: '...total revenue grew 12% year over year...',
  matched_in: 'message',
  updated_at: new Date().toISOString(),
}

describe('ConversationSearchResults', () => {
  it('shows a spinner while searching with no results yet', () => {
    render(<ConversationSearchResults results={null} isSearching={true} error={null} onSelect={vi.fn()} />)
    expect(document.querySelector('.animate-spin')).toBeInTheDocument()
  })

  it('shows an error message with role=alert', () => {
    render(
      <ConversationSearchResults results={null} isSearching={false} error="Search failed." onSelect={vi.fn()} />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Search failed.')
  })

  it('shows an empty state when results is an empty array', () => {
    render(<ConversationSearchResults results={[]} isSearching={false} error={null} onSelect={vi.fn()} />)
    expect(screen.getByText(/no conversations match/i)).toBeInTheDocument()
  })

  it('renders each hit with its title and snippet, and calls onSelect', async () => {
    const onSelect = vi.fn()
    render(<ConversationSearchResults results={[hit]} isSearching={false} error={null} onSelect={onSelect} />)
    expect(screen.getByText('Revenue last quarter')).toBeInTheDocument()
    expect(screen.getByText(/total revenue grew 12%/)).toBeInTheDocument()
    await userEvent.click(screen.getByText('Revenue last quarter'))
    expect(onSelect).toHaveBeenCalledWith(hit)
  })
})
