import { request } from './api'
import type {
  RecommendationFeedbackEventOut,
  RecommendationQualityMetricsOut,
  RecommendationRecordOut,
  RecommendationVerdict,
} from './types'

/** Thin wrappers around `/recommendations/*` (`api/recommendation_governance.py`,
 * Prompts 18 and 31). Every call is scoped server-side to the caller's own
 * tenant -- no function here accepts a tenant id, and a cross-tenant record
 * id is indistinguishable from a nonexistent one (both 404). */

export interface RecommendationListFilters {
  databaseId?: string
  category?: string
  status?: string
  /** `unassigned` wins over `ownerUserId` on the server; send one. */
  ownerUserId?: string
  unassigned?: boolean
}

export function listRecommendations(
  filters: RecommendationListFilters = {},
): Promise<RecommendationRecordOut[]> {
  const params = new URLSearchParams()
  if (filters.databaseId) params.set('database_id', filters.databaseId)
  if (filters.category) params.set('category', filters.category)
  if (filters.status) params.set('status', filters.status)
  if (filters.unassigned) params.set('unassigned', 'true')
  else if (filters.ownerUserId) params.set('owner_user_id', filters.ownerUserId)
  const query = params.toString()
  return request<RecommendationRecordOut[]>(`/recommendations${query ? `?${query}` : ''}`)
}

export function getRecommendation(recordId: string): Promise<RecommendationRecordOut> {
  return request<RecommendationRecordOut>(`/recommendations/${recordId}`)
}

export function listRecommendationEvents(recordId: string): Promise<RecommendationFeedbackEventOut[]> {
  return request<RecommendationFeedbackEventOut[]>(`/recommendations/${recordId}/events`)
}

export function submitRecommendationVerdict(
  recordId: string,
  payload: { status: RecommendationVerdict; reason?: string | null },
): Promise<RecommendationRecordOut> {
  return request<RecommendationRecordOut>(`/recommendations/${recordId}/feedback`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function resolveRecommendation(
  recordId: string,
  reason: string | null = null,
): Promise<RecommendationRecordOut> {
  return request<RecommendationRecordOut>(`/recommendations/${recordId}/resolve`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function expireRecommendation(
  recordId: string,
  reason: string | null = null,
): Promise<RecommendationRecordOut> {
  return request<RecommendationRecordOut>(`/recommendations/${recordId}/expire`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function addRecommendationNote(
  recordId: string,
  note: string,
): Promise<RecommendationFeedbackEventOut> {
  return request<RecommendationFeedbackEventOut>(`/recommendations/${recordId}/notes`, {
    method: 'POST',
    body: JSON.stringify({ note }),
  })
}

/** `ownerUserId: null` clears the owner. */
export function assignRecommendationOwner(
  recordId: string,
  ownerUserId: string | null,
): Promise<RecommendationRecordOut> {
  return request<RecommendationRecordOut>(`/recommendations/${recordId}/owner`, {
    method: 'POST',
    body: JSON.stringify({ owner_user_id: ownerUserId }),
  })
}

export function getRecommendationQualityMetrics(): Promise<RecommendationQualityMetricsOut> {
  return request<RecommendationQualityMetricsOut>('/recommendations/metrics')
}
