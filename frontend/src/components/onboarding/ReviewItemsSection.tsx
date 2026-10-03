import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { OnboardingReviewItem } from '@/lib/types'

/** One table of onboarding review items (PII candidates, relationship
 * candidates, semantic labels, or golden questions -- same shape,
 * different `item_type`), with inline confirm/reject actions. Shared by
 * every tab in `DatabaseOnboarding.tsx` rather than four near-duplicate
 * tables, since `OnboardingReviewItem`'s shape is already identical
 * across all four types (`api/onboarding_schemas.py`'s own design).
 *
 * `canReview` is a UX-only gate (hides the Confirm/Reject buttons for a
 * viewer without ONBOARDING_REVIEW) -- the real enforcement is server-side
 * (`POST /onboarding/jobs/{id}/review-items/{id}/decide` itself checks the
 * caller's permission); a viewer with the buttons hidden here still could
 * not call that endpoint successfully by, say, opening dev tools, since
 * nothing client-side grants a permission the server doesn't already have
 * on record for that account. */
export function ReviewItemsSection({
  items,
  canReview,
  onDecide,
  pendingItemId,
  emptyMessage,
}: {
  items: OnboardingReviewItem[]
  canReview: boolean
  onDecide: (itemId: string, decision: 'confirmed' | 'rejected') => void
  pendingItemId: string | null
  emptyMessage: string
}) {
  const [showOnlyAmbiguous, setShowOnlyAmbiguous] = useState(false)
  const ambiguousCount = items.filter((item) => item.is_ambiguous).length
  const visibleItems = showOnlyAmbiguous ? items.filter((item) => item.is_ambiguous) : items

  if (items.length === 0) {
    return <p className="py-6 text-center text-sm text-[var(--muted-foreground)]">{emptyMessage}</p>
  }

  return (
    <div className="flex flex-col gap-3">
      {ambiguousCount > 0 && (
        <label className="flex items-center gap-2 text-sm text-[var(--muted-foreground)]">
          <input
            type="checkbox"
            checked={showOnlyAmbiguous}
            onChange={(event) => setShowOnlyAmbiguous(event.target.checked)}
            className="h-4 w-4 rounded border-[var(--border)]"
          />
          Show only ambiguous items ({ambiguousCount} of {items.length})
        </label>
      )}
      <div className="overflow-x-auto rounded-lg border border-[var(--border)]">
        <table className="w-full text-left text-sm">
          <thead className="bg-[var(--muted)] text-xs uppercase text-[var(--muted-foreground)]">
            <tr>
              <th className="px-3 py-2 font-medium">Subject</th>
              <th className="px-3 py-2 font-medium">Confidence</th>
              <th className="px-3 py-2 font-medium">Status</th>
              {canReview && <th className="px-3 py-2 font-medium">Decide</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border)]">
            {visibleItems.map((item) => (
              <tr key={item.id}>
                <td className="max-w-md px-3 py-2">
                  <span className="font-mono text-xs">{item.subject}</span>
                  {item.is_ambiguous && (
                    <Badge tone="warning" className="ml-2">
                      ambiguous
                    </Badge>
                  )}
                </td>
                <td className="px-3 py-2 tabular-nums">{(item.confidence * 100).toFixed(0)}%</td>
                <td className="px-3 py-2">
                  <DecisionBadge decision={item.decision} />
                </td>
                {canReview && (
                  <td className="px-3 py-2">
                    {item.decision === 'pending' ? (
                      <div className="flex gap-1.5">
                        <Button
                          size="sm"
                          variant="secondary"
                          disabled={pendingItemId === item.id}
                          onClick={() => onDecide(item.id, 'confirmed')}
                        >
                          Confirm
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={pendingItemId === item.id}
                          onClick={() => onDecide(item.id, 'rejected')}
                        >
                          Reject
                        </Button>
                      </div>
                    ) : (
                      <span className="text-xs text-[var(--muted-foreground)]">
                        {item.decided_at ? new Date(item.decided_at).toLocaleString() : ''}
                      </span>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function DecisionBadge({ decision }: { decision: OnboardingReviewItem['decision'] }) {
  if (decision === 'confirmed') return <Badge tone="success">confirmed</Badge>
  if (decision === 'rejected') return <Badge tone="danger">rejected</Badge>
  return <Badge tone="neutral">pending</Badge>
}
