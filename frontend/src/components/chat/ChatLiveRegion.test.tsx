import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PendingQuestion } from '@/store/chatStore'
import { ChatLiveRegion } from './ChatLiveRegion'

describe('ChatLiveRegion', () => {
  it('announces nothing on initial mount (loading an existing conversation must stay silent)', () => {
    render(<ChatLiveRegion pendingQuestion={null} latestEntryId="e1" />)
    expect(screen.getByRole('status')).toHaveTextContent('')
  })

  it('announces "thinking" once a question starts pending', () => {
    const pending: PendingQuestion = { question: 'Revenue?', startedAt: 0 }
    const { rerender } = render(<ChatLiveRegion pendingQuestion={null} latestEntryId={null} />)
    rerender(<ChatLiveRegion pendingQuestion={pending} latestEntryId={null} />)
    expect(screen.getByRole('status')).toHaveTextContent(/thinking/i)
  })

  it('announces the answer once pending clears and a new entry lands', () => {
    const pending: PendingQuestion = { question: 'Revenue?', startedAt: 0 }
    const { rerender } = render(<ChatLiveRegion pendingQuestion={pending} latestEntryId={null} />)
    rerender(<ChatLiveRegion pendingQuestion={null} latestEntryId="e1" />)
    expect(screen.getByRole('status')).toHaveTextContent(/answer is ready/i)
  })

  it('does not re-announce when latestEntryId is unchanged', () => {
    const { rerender } = render(<ChatLiveRegion pendingQuestion={null} latestEntryId="e1" />)
    rerender(<ChatLiveRegion pendingQuestion={null} latestEntryId="e1" />)
    expect(screen.getByRole('status')).toHaveTextContent('')
  })
})
