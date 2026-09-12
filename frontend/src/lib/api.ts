import type {
  AskRequest,
  AskResponse,
  Collection,
  DocumentListResponse,
  DocumentUploadResponse,
  ExecuteRequest,
  ExecuteResponse,
  GoldenExampleFeedbackRequest,
  HealthResponse,
  SchemaRefreshResponse,
  SensitivityCategory,
  TablesResponse,
} from './types'

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

// Set at build time (VITE_API_AUTH_TOKEN) only if the backend is configured
// with API_AUTH_TOKEN -- most local/single-user deployments leave both
// unset, matching ui/app.py's own no-auth default (see api/auth.py).
const API_TOKEN = import.meta.env.VITE_API_AUTH_TOKEN as string | undefined

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (API_TOKEN) headers.set('Authorization', `Bearer ${API_TOKEN}`)
  if (init?.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(path, { ...init, headers })
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}.`
    try {
      const body = (await response.clone().json()) as { detail?: string }
      detail = body.detail ?? detail
    } catch {
      // Non-JSON error body (e.g. a 429 from a proxy in front of the app) --
      // the generic message above is still safe to show.
    }
    throw new ApiError(detail, response.status)
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export function askQuestion(payload: AskRequest): Promise<AskResponse> {
  return request<AskResponse>('/ask', { method: 'POST', body: JSON.stringify(payload) })
}

export function executeSql(payload: ExecuteRequest): Promise<ExecuteResponse> {
  return request<ExecuteResponse>('/execute', { method: 'POST', body: JSON.stringify(payload) })
}

export function submitGoldenExampleFeedback(
  payload: GoldenExampleFeedbackRequest,
): Promise<{ saved: boolean }> {
  return request('/feedback/golden-example', { method: 'POST', body: JSON.stringify(payload) })
}

export function refreshSchema(): Promise<SchemaRefreshResponse> {
  return request<SchemaRefreshResponse>('/schema/refresh', { method: 'POST' })
}

export function getSchemaTables(database?: string): Promise<TablesResponse> {
  const query = database ? `?database=${encodeURIComponent(database)}` : ''
  return request<TablesResponse>(`/schema/tables${query}`)
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health')
}

export function listDocuments(collection?: Collection): Promise<DocumentListResponse> {
  const query = collection ? `?collection=${collection}` : ''
  return request<DocumentListResponse>(`/documents${query}`)
}

export function uploadDocument(
  file: File,
  collection: Collection,
  sensitivityCategory?: SensitivityCategory,
): Promise<DocumentUploadResponse> {
  const form = new FormData()
  form.append('file', file)
  form.append('collection', collection)
  if (sensitivityCategory) form.append('sensitivity_category', sensitivityCategory)
  return request<DocumentUploadResponse>('/documents', { method: 'POST', body: form })
}

export function deleteDocument(documentId: string): Promise<void> {
  return request<void>(`/documents/${documentId}`, { method: 'DELETE' })
}

export async function downloadDocument(documentId: string, filename: string): Promise<void> {
  const headers = new Headers()
  if (API_TOKEN) headers.set('Authorization', `Bearer ${API_TOKEN}`)
  const response = await fetch(`/documents/${documentId}/download`, { headers })
  if (!response.ok) {
    throw new ApiError(`Could not download ${filename}.`, response.status)
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

/** Fetches one generated image/video's bytes and returns a blob object URL
 * suitable for an `<img>`/`<video>` `src` -- unlike `downloadDocument`,
 * this doesn't trigger a browser download, it's for inline rendering
 * (`MediaResultCard`). A plain `<img src="/media/{id}">` can't carry the
 * `Authorization` header this route requires, so the bytes must be
 * fetched here and turned into a same-origin blob URL instead. Callers
 * must `URL.revokeObjectURL` the result once it's no longer displayed. */
export async function fetchMediaBlobUrl(mediaId: string): Promise<string> {
  const headers = new Headers()
  if (API_TOKEN) headers.set('Authorization', `Bearer ${API_TOKEN}`)
  const response = await fetch(`/media/${mediaId}`, { headers })
  if (!response.ok) {
    throw new ApiError('Could not load the generated media.', response.status)
  }
  const blob = await response.blob()
  return URL.createObjectURL(blob)
}
