import type {
  RecommendationCategory,
  RecommendationFeedbackEventOut,
  RecommendationRecordOut,
  RecommendationStatus,
} from './types'

/** Pure display rules for the recommendation dashboard (Prompt 31). Kept free
 * of React and of any network call so they can be unit-tested directly.
 *
 * **These mirror, they do not enforce.** The backend's
 * `recommendation/governance.py::VALID_STATUS_TRANSITIONS` is the one source
 * of truth for which transition is legal, and it re-checks every request. The
 * helpers here only decide which buttons to *show*, so a reviewer is not
 * offered an action that would be refused. A stale button still gets a clean
 * 409 from the server. */

export const CATEGORY_LABELS: Record<RecommendationCategory, string> = {
  performance: 'Performance',
  anomaly: 'Anomaly',
  revenue: 'Revenue',
  customer: 'Customer',
  product: 'Product',
  operations: 'Operations',
  data_quality: 'Data quality',
  security: 'Security',
  database_performance: 'Database performance',
}

export const STATUS_LABELS: Record<RecommendationStatus, string> = {
  generated: 'Awaiting review',
  reviewed: 'Reviewed',
  accepted: 'Accepted',
  rejected: 'Rejected',
  partially_useful: 'Partially useful',
  incorrect: 'Incorrect',
  resolved: 'Resolved',
  expired: 'Expired',
}

export type StatusTone = 'neutral' | 'success' | 'warning' | 'danger' | 'accent'

export function statusTone(status: RecommendationStatus): StatusTone {
  switch (status) {
    case 'accepted':
    case 'resolved':
      return 'success'
    case 'partially_useful':
    case 'reviewed':
      return 'warning'
    case 'rejected':
    case 'incorrect':
      return 'danger'
    case 'generated':
      return 'accent'
    case 'expired':
      return 'neutral'
  }
}

/** A terminal status has no legal outgoing transition at all. */
export function isTerminal(status: RecommendationStatus): boolean {
  return status === 'rejected' || status === 'incorrect' || status === 'resolved' || status === 'expired'
}

/** Verdicts a reviewer can still record (the backend allows these from
 * `generated` and `reviewed`). */
export function canRecordVerdict(status: RecommendationStatus): boolean {
  return status === 'generated' || status === 'reviewed'
}

/** Only an accepted or partially-useful record can be marked resolved. */
export function canResolve(status: RecommendationStatus): boolean {
  return status === 'accepted' || status === 'partially_useful'
}

/** Force-expiring is an administrator's override of any non-terminal record. */
export function canExpire(status: RecommendationStatus): boolean {
  return !isTerminal(status)
}

export function formatConfidence(confidence: number | null): string {
  if (confidence === null || !Number.isFinite(confidence)) return 'Not scored'
  return `${Math.round(confidence * 100)}%`
}

export function formatWhen(iso: string): string {
  const parsed = new Date(iso)
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString()
}

/** One human-readable line per audit event. Notes and owner changes carry
 * no status change, so they are described by their own kind rather than as
 * "from X to X". */
export function describeEvent(event: RecommendationFeedbackEventOut): string {
  const kind = event.event_type ?? 'status_change'
  if (kind === 'note') return 'Note added'
  if (kind === 'owner_assigned') {
    return event.detail?.owner_user_id ? 'Owner assigned' : 'Owner cleared'
  }
  if (event.from_status === null) return 'Generated'
  return `${STATUS_LABELS[event.from_status]} → ${STATUS_LABELS[event.to_status]}`
}

export type OwnershipFilter = 'anyone' | 'mine' | 'unassigned'

/** The owner name shown for a record, or `null` when there is nothing honest
 * to show. Never an email address, never a guessed name. */
export function ownerLabel(record: Pick<RecommendationRecordOut, 'owner_user_id' | 'owner_display_name'>): string {
  if (!record.owner_user_id) return 'Unassigned'
  return record.owner_display_name ?? 'Assigned to a reviewer'
}
