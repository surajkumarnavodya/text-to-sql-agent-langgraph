import { request } from './api'
import type {
  CatalogEntryOut,
  CreateCatalogEntryRequest,
  ReviewDecisionRequest,
  UpdateCatalogEntryRequest,
} from './types'

/** Thin wrappers around `/semantic-catalog/*` (`api/semantic_catalog.py`,
 * Prompt 09/10/27) -- the governed business-concept catalog (entities/
 * metrics/dimensions/domains). Mirrors `lib/onboardingApi.ts`'s own
 * established convention exactly: every function is a bare `request()`
 * call with no extra logic of its own. */

export function createCatalogEntry(payload: CreateCatalogEntryRequest): Promise<CatalogEntryOut> {
  return request<CatalogEntryOut>('/semantic-catalog/entries', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function listCatalogEntries(params?: {
  databaseId?: string
  conceptType?: string
  status?: string
  includeConflicts?: boolean
}): Promise<CatalogEntryOut[]> {
  const query = new URLSearchParams()
  if (params?.databaseId) query.set('database_id', params.databaseId)
  if (params?.conceptType) query.set('concept_type', params.conceptType)
  if (params?.status) query.set('status', params.status)
  if (params?.includeConflicts) query.set('include_conflicts', 'true')
  const suffix = query.toString() ? `?${query.toString()}` : ''
  return request<CatalogEntryOut[]>(`/semantic-catalog/entries${suffix}`)
}

export function getCatalogEntry(entryId: string): Promise<CatalogEntryOut> {
  return request<CatalogEntryOut>(`/semantic-catalog/entries/${entryId}`)
}

export function getCatalogEntryVersions(params: {
  conceptKey: string
  databaseId: string
  conceptType: string
}): Promise<CatalogEntryOut[]> {
  const query = new URLSearchParams({
    database_id: params.databaseId,
    concept_type: params.conceptType,
  })
  return request<CatalogEntryOut[]>(
    `/semantic-catalog/entries/concept/${params.conceptKey}/versions?${query.toString()}`,
  )
}

export function updateCatalogEntry(
  entryId: string,
  payload: UpdateCatalogEntryRequest,
): Promise<CatalogEntryOut> {
  return request<CatalogEntryOut>(`/semantic-catalog/entries/${entryId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export function reviewCatalogEntry(
  entryId: string,
  payload: ReviewDecisionRequest,
): Promise<CatalogEntryOut> {
  return request<CatalogEntryOut>(`/semantic-catalog/entries/${entryId}/review`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function requestCatalogEntryChanges(
  entryId: string,
  payload: ReviewDecisionRequest,
): Promise<CatalogEntryOut> {
  return request<CatalogEntryOut>(`/semantic-catalog/entries/${entryId}/request-changes`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function publishCatalogEntry(entryId: string): Promise<CatalogEntryOut> {
  return request<CatalogEntryOut>(`/semantic-catalog/entries/${entryId}/publish`, {
    method: 'POST',
  })
}
