import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ConversationSearch } from './ConversationSearch'

describe('ConversationSearch', () => {
  it('renders the current value and calls onChange as the user types', async () => {
    const onChange = vi.fn()
    render(<ConversationSearch value="" onChange={onChange} isSearching={false} />)
    const input = screen.getByRole('searchbox', { name: /search conversations/i })
    await userEvent.type(input, 'revenue')
    expect(onChange).toHaveBeenCalled()
  })

  it('shows a clear button only when there is a value, and clears on click', async () => {
    const onChange = vi.fn()
    const { rerender } = render(<ConversationSearch value="" onChange={onChange} isSearching={false} />)
    expect(screen.queryByRole('button', { name: /clear/i })).not.toBeInTheDocument()

    rerender(<ConversationSearch value="revenue" onChange={onChange} isSearching={false} />)
    await userEvent.click(screen.getByRole('button', { name: /clear/i }))
    expect(onChange).toHaveBeenCalledWith('')
  })

  it('shows a spinner instead of the clear button while searching', () => {
    render(<ConversationSearch value="revenue" onChange={vi.fn()} isSearching={true} />)
    expect(screen.queryByRole('button', { name: /clear/i })).not.toBeInTheDocument()
  })

  it('clears on Escape when there is a value', async () => {
    const onChange = vi.fn()
    render(<ConversationSearch value="revenue" onChange={onChange} isSearching={false} />)
    const input = screen.getByRole('searchbox')
    input.focus()
    await userEvent.keyboard('{Escape}')
    expect(onChange).toHaveBeenCalledWith('')
  })
})
