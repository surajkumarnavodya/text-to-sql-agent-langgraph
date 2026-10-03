import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { OnboardingReviewItem } from '@/lib/types'
import { ReviewItemsSection } from './ReviewItemsSection'

function makeItem(overrides: Partial<OnboardingReviewItem> = {}): OnboardingReviewItem {
  return {
    id: 'item-1',
    item_type: 'semantic_label',
    table_name: 'orders',
    column_name: 'customer_id',
    subject: 'orders.customer_id -> foreign_key',
    payload: {},
    confidence: 0.85,
    is_ambiguous: false,
    decision: 'pending',
    decided_at: null,
    decision_notes: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('ReviewItemsSection', () => {
  it('renders the empty-state message when there are no items', () => {
    render(
      <ReviewItemsSection
        items={[]}
        canReview
        onDecide={vi.fn()}
        pendingItemId={null}
        emptyMessage="Nothing to review here."
      />,
    )
    expect(screen.getByText('Nothing to review here.')).toBeInTheDocument()
  })

  it('shows Confirm/Reject buttons only when canReview is true', () => {
    const { rerender } = render(
      <ReviewItemsSection
        items={[makeItem()]}
        canReview={false}
        onDecide={vi.fn()}
        pendingItemId={null}
        emptyMessage="none"
      />,
    )
    expect(screen.queryByRole('button', { name: /confirm/i })).not.toBeInTheDocument()

    rerender(
      <ReviewItemsSection
        items={[makeItem()]}
        canReview
        onDecide={vi.fn()}
        pendingItemId={null}
        emptyMessage="none"
      />,
    )
    expect(screen.getByRole('button', { name: /confirm/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /reject/i })).toBeInTheDocument()
  })

  it('calls onDecide with the item id and decision when Confirm/Reject is clicked', async () => {
    const onDecide = vi.fn()
    render(
      <ReviewItemsSection
        items={[makeItem({ id: 'abc' })]}
        canReview
        onDecide={onDecide}
        pendingItemId={null}
        emptyMessage="none"
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: /confirm/i }))
    expect(onDecide).toHaveBeenCalledWith('abc', 'confirmed')

    await userEvent.click(screen.getByRole('button', { name: /reject/i }))
    expect(onDecide).toHaveBeenCalledWith('abc', 'rejected')
  })

  it('does not show Confirm/Reject for an already-decided item', () => {
    render(
      <ReviewItemsSection
        items={[makeItem({ decision: 'confirmed', decided_at: '2026-01-02T00:00:00Z' })]}
        canReview
        onDecide={vi.fn()}
        pendingItemId={null}
        emptyMessage="none"
      />,
    )
    expect(screen.queryByRole('button', { name: /confirm/i })).not.toBeInTheDocument()
    expect(screen.getByText('confirmed')).toBeInTheDocument()
  })

  it('filters to only ambiguous items when the checkbox is toggled', async () => {
    render(
      <ReviewItemsSection
        items={[
          makeItem({ id: 'clear', subject: 'clear-subject', is_ambiguous: false }),
          makeItem({ id: 'fuzzy', subject: 'fuzzy-subject', is_ambiguous: true }),
        ]}
        canReview
        onDecide={vi.fn()}
        pendingItemId={null}
        emptyMessage="none"
      />,
    )

    expect(screen.getByText('clear-subject')).toBeInTheDocument()
    expect(screen.getByText('fuzzy-subject')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('checkbox', { name: /show only ambiguous/i }))

    expect(screen.queryByText('clear-subject')).not.toBeInTheDocument()
    expect(screen.getByText('fuzzy-subject')).toBeInTheDocument()
  })

  it('disables Confirm/Reject only for the item currently being decided', () => {
    render(
      <ReviewItemsSection
        items={[makeItem({ id: 'a' }), makeItem({ id: 'b', subject: 'second' })]}
        canReview
        onDecide={vi.fn()}
        pendingItemId="a"
        emptyMessage="none"
      />,
    )
    const confirmButtons = screen.getAllByRole('button', { name: /confirm/i })
    expect(confirmButtons[0]).toBeDisabled()
    expect(confirmButtons[1]).not.toBeDisabled()
  })
})
