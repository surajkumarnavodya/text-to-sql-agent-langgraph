import { getBearerToken } from '@/store/authStore'
import type {
  AiImageEditRequest,
  AiImageEditResponse,
  AskRequest,
  AskResponse,
  AttachmentCapabilities,
  AttachmentUploadResponse,
  BlurRegionRequest,
  Collection,
  DetectTextRegionsResponse,
  DocumentListResponse,
  DocumentUploadResponse,
  ExecuteRequest,
  ExecuteResponse,
  GoldenExampleFeedbackRequest,
  HealthResponse,
  ImageEditResultResponse,
  ImageRegionIn,
  ImageResizeRequest,
  MediaGenerationResult,
  MediaSearchResult,
  MessageFeedbackRequest,
  ModelsResponse,
  OcrExtractResponse,
  SchemaRefreshResponse,
  SensitivityCategory,
  TablesResponse,
  TranscribeResponse,
} from './types'

/** FastAPI's ordinary error body is `{ detail: string }`, but a 422
 * validation failure's `detail` is instead an array of Pydantic error
 * objects (`{ type, loc, msg, ... }`) -- and a proxy/gateway in front of
 * this app could plausibly return some other non-string shape entirely.
 * Naively doing `body.detail ?? fallback` and handing the result straight
 * to `ApiError`/`Error` let a non-string `detail` reach the UI as the
 * literal text `"[object Object]"` (a real, reported bug): an array's own
 * `.toString()` calls `.toString()` on each element, and a plain object's
 * default `.toString()` is always exactly that string, so whichever
 * component eventually rendered `error.message` as text showed that
 * instead of anything readable. This normalizes every shape `request()`
 * might see into a safe, human-readable string -- never a raw object, and
 * never a stack trace/internal detail beyond what the server already
 * chose to put in `detail`. */
function normalizeErrorDetail(detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail) && detail.length > 0) {
    const messages = detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && typeof (item as { msg?: unknown }).msg === 'string') {
          return (item as { msg: string }).msg
        }
        return null
      })
      .filter((message): message is string => message !== null)
    if (messages.length > 0) return messages.join(' ')
  }
  return fallback
}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** Registered by `AuthGate.tsx` (`localAuthStore.handleUnauthorized`) once
 * on app mount -- kept as a setter rather than a direct import so this
 * module never has to import the store (which already imports this
 * module, via `identityApi.ts`'s own `request()` reuse). A real gap found
 * via live use: `access_token_expire_minutes` defaults to 15 minutes
 * (`config/settings.py`), and before this existed nothing in the frontend
 * noticed when that access token went stale mid-session -- every call
 * failed with a bare "Missing or invalid Authorization header." until the
 * user manually reloaded the page (the only thing that re-ran
 * `localAuthStore.initialize()`'s own refresh-via-cookie check). Returning
 * `null` (no live session to recover) is a normal, expected outcome, not
 * an error -- `request()` just lets the original 401 propagate then. */
type UnauthorizedHandler = () => Promise<string | null>
let unauthorizedHandler: UnauthorizedHandler | undefined
export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  unauthorizedHandler = handler
}

// Shared across every concurrent 401 so a burst of simultaneous requests
// (easily hit in practice -- this app fires several independent calls on
// load) triggers exactly one `/auth/refresh` call, not one per request.
let refreshInFlight: Promise<string | null> | null = null

/** Exported so `frontend/src/lib/identityApi.ts` can reuse the exact same
 * fetch/error-handling/auth-header logic for `/auth/*` calls, rather than
 * duplicating it -- every other function in this file is just a thin
 * wrapper around this one.
 *
 * `overrideToken` is internal-only (the retry-after-refresh path sets it) --
 * every real call site omits it. Passed explicitly rather than relying on
 * the retry's own `getBearerToken()` call to pick up whatever
 * `unauthorizedHandler` just wrote to its store: that's true in production
 * (`localAuthStore.handleUnauthorized` does update the store before
 * returning), but making the retry depend on that ordering as an implicit
 * side effect, rather than just using the token already in hand, is a
 * needless, fragile coupling between two separate modules. */
export async function request<T>(
  path: string,
  init?: RequestInit,
  overrideToken?: string,
): Promise<T> {
  const isRetryAfterRefresh = overrideToken !== undefined
  const headers = new Headers(init?.headers)
  const token = overrideToken ?? getBearerToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init?.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(path, { ...init, headers })

  // `/auth/*` itself is deliberately exempt -- `/auth/refresh` failing its
  // own 401 must never re-trigger another refresh attempt (infinite
  // recursion), and a `/auth/login` 401 is a real "wrong password," not a
  // stale-token condition a silent refresh could ever fix.
  if (
    response.status === 401 &&
    !isRetryAfterRefresh &&
    !path.startsWith('/auth/') &&
    unauthorizedHandler
  ) {
    refreshInFlight ??= unauthorizedHandler().finally(() => {
      refreshInFlight = null
    })
    const refreshedToken = await refreshInFlight
    if (refreshedToken) {
      return request<T>(path, init, refreshedToken)
    }
  }

  if (!response.ok) {
    const fallback = `Request failed with status ${response.status}.`
    let detail = fallback
    try {
      const body = (await response.clone().json()) as { detail?: unknown }
      detail = normalizeErrorDetail(body.detail, fallback)
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

export function askQuestion(payload: AskRequest, signal?: AbortSignal): Promise<AskResponse> {
  return request<AskResponse>('/ask', { method: 'POST', body: JSON.stringify(payload), signal })
}

export function executeSql(payload: ExecuteRequest): Promise<ExecuteResponse> {
  return request<ExecuteResponse>('/execute', { method: 'POST', body: JSON.stringify(payload) })
}

/** The human-approval confirmation step for media generation -- the
 * "generation" source equivalent of `executeSql` above. `question` is
 * normally the exact text that produced the `status: 'pending_approval'`
 * proposal (see MediaResultCard.tsx); the server re-infers image-vs-video
 * from it and only then makes the real, metered IMA Studio call. */
export function confirmGeneration(question: string): Promise<MediaGenerationResult> {
  return request<MediaGenerationResult>('/generate/confirm', {
    method: 'POST',
    body: JSON.stringify({ question }),
  })
}

export function submitGoldenExampleFeedback(
  payload: GoldenExampleFeedbackRequest,
): Promise<{ saved: boolean }> {
  return request('/feedback/golden-example', { method: 'POST', body: JSON.stringify(payload) })
}

/** General like/dislike + optional comment on any answer -- see
 * ResponseFeedbackWidget.tsx. Independent of submitGoldenExampleFeedback
 * above, which only ever fires for a confirmed SQL result. */
export function submitMessageFeedback(payload: MessageFeedbackRequest): Promise<{ saved: boolean }> {
  return request('/feedback/message', { method: 'POST', body: JSON.stringify(payload) })
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

/** The Ollama Text-to-SQL model registry (`GET /models`) -- every
 * configured/allowed model enriched with live "is it installed right now"
 * status. See useAvailableModels() (react-query, cached like useHealth) and
 * HistorySettingsSection.tsx's "AI Model" picker. Never called on every
 * /ask -- the frontend caches this response instead. */
export function getAvailableModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>('/models')
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

/** Uploads one or more chat attachments -- validates, malware-scans,
 * stores, and eagerly processes each file server-side, returning per-file
 * status/errors (see useChatAttachments.ts, api/attachments.py). A file's
 * `attachment_id` in the response is what gets passed as
 * `AskRequest.attachment_ids`. */
export function uploadAttachments(files: File[]): Promise<AttachmentUploadResponse> {
  const form = new FormData()
  for (const file of files) form.append('files', file)
  return request<AttachmentUploadResponse>('/attachments/upload', { method: 'POST', body: form })
}

export function deleteAttachment(attachmentId: string): Promise<void> {
  return request<void>(`/attachments/${attachmentId}`, { method: 'DELETE' })
}

/** What this deployment can actually do with an attachment right now
 * (`GET /attachments/capabilities`) -- vision/OCR/resize/text-removal
 * availability, size limits, resize presets. See AttachmentCapabilities's
 * own docstring for why the composer's image-action menu must read this
 * rather than assuming every action is always available. */
export function getAttachmentCapabilities(): Promise<AttachmentCapabilities> {
  return request<AttachmentCapabilities>('/attachments/capabilities')
}

/** "Extract text" -- runs real OCR (Tesseract) server-side and returns
 * exactly what it recognized, never a vision-model paraphrase. Distinct
 * from asking a natural-language question about an image via `/ask`. */
export function extractAttachmentText(attachmentId: string): Promise<OcrExtractResponse> {
  return request<OcrExtractResponse>(`/attachments/${attachmentId}/extract-text`, { method: 'POST' })
}

/** Proposes OCR-detected text-line regions for the "Remove text" workflow's
 * confirm/adjust step -- no pixels are edited by this call. */
export function detectAttachmentTextRegions(attachmentId: string): Promise<DetectTextRegionsResponse> {
  return request<DetectTextRegionsResponse>(`/attachments/${attachmentId}/detect-text-regions`)
}

/** "Remove text" -- real pixel editing via classical (OpenCV) inpainting,
 * never a solid rectangle or a CSS overlay. `regions` is either
 * OCR-proposed (caller-confirmed) or manually drawn, in source-image pixel
 * coordinates. Always returns a brand-new attachment; the original is
 * untouched. */
export function removeAttachmentText(
  attachmentId: string,
  regions: ImageRegionIn[],
): Promise<ImageEditResultResponse> {
  return request<ImageEditResultResponse>(`/attachments/${attachmentId}/remove-text`, {
    method: 'POST',
    body: JSON.stringify({ regions }),
  })
}

/** "Resize" -- deterministic Pillow work, no model call. Always returns a
 * brand-new attachment; if its `attachment_id` is then attached to a
 * follow-up question, the *resized* bytes are what reach the model. */
export function resizeAttachmentImage(
  attachmentId: string,
  body: ImageResizeRequest,
): Promise<ImageEditResultResponse> {
  return request<ImageEditResultResponse>(`/attachments/${attachmentId}/resize`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** "Blur region" -- deterministic, local Pillow Gaussian blur over a
 * painted mask, never a model call. Works even when AI-guided editing is
 * disabled (see api/attachments.py's own route docstring). Always returns
 * a brand-new attachment, matching resize/remove-text's own contract. */
export function blurAttachmentRegion(
  attachmentId: string,
  body: BlurRegionRequest,
): Promise<ImageEditResultResponse> {
  return request<ImageEditResultResponse>(`/attachments/${attachmentId}/blur-region`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** Real, generative AI-guided image editing (`POST
 * /attachments/{id}/ai-edit`) -- the endpoint that used to not exist at
 * all. `attachmentId` must be a fresh, transient attachment holding the
 * editor's *current* canvas export (see `lib/imageEditAdapter.ts`'s own
 * docstring for why). Never throws for an ordinary "the edit didn't work"
 * outcome -- check `response.status` instead; `request()` only rejects for
 * a genuine transport/auth/validation failure. */
export function aiEditAttachmentImage(
  attachmentId: string,
  body: AiImageEditRequest,
): Promise<AiImageEditResponse> {
  return request<AiImageEditResponse>(`/attachments/${attachmentId}/ai-edit`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function downloadDocument(documentId: string, filename: string): Promise<void> {
  const headers = new Headers()
  const token = getBearerToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
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
  const token = getBearerToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`/media/${mediaId}`, { headers })
  if (!response.ok) {
    throw new ApiError('Could not load the generated media.', response.status)
  }
  const blob = await response.blob()
  return URL.createObjectURL(blob)
}

/** Fetches one media-*library* asset's bytes (an ingested image, or a
 * video segment's representative frame) -- distinct from
 * `fetchMediaBlobUrl` above, which is for ephemeral *generated* media
 * (`GET /media/{id}`). This hits the persistent library route instead
 * (`GET /media/library/{id}`, `api/media_library.py`). */
export async function fetchLibraryMediaBlobUrl(mediaId: string): Promise<string> {
  const headers = new Headers()
  const token = getBearerToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`/media/library/${mediaId}`, { headers })
  if (!response.ok) {
    throw new ApiError('Could not load that media item.', response.status)
  }
  const blob = await response.blob()
  return URL.createObjectURL(blob)
}

/** Direct media-library search, independent of the conversational `/ask`
 * flow (`POST /search/media`, `api/media_search.py`). */
export function searchMedia(
  query: string,
  mediaType: 'image' | 'video' | 'any' = 'any',
): Promise<MediaSearchResult> {
  return request<MediaSearchResult>('/search/media', {
    method: 'POST',
    body: JSON.stringify({ query, media_type: mediaType }),
  })
}

/** Uploads one recorded question for local transcription (`voice/stt.py`).
 * The returned text is plain, untrusted input -- callers must submit it
 * back through `askQuestion` like any typed question, never treat it as
 * pre-validated. */
export function transcribeAudio(audio: Blob): Promise<TranscribeResponse> {
  const form = new FormData()
  form.append('audio', audio, 'question.webm')
  return request<TranscribeResponse>('/voice/transcribe', { method: 'POST', body: form })
}

/** Synthesizes an answer to speech (`voice/tts.py`) and returns a blob
 * object URL suitable for an `<audio>` `src` -- same reasoning as
 * `fetchMediaBlobUrl` above (the endpoint needs an Authorization header a
 * plain `<audio src="...">` can't send). Callers must
 * `URL.revokeObjectURL` the result once playback is done. */
export async function synthesizeSpeechUrl(text: string): Promise<string> {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  const token = getBearerToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch('/voice/synthesize', {
    method: 'POST',
    headers,
    body: JSON.stringify({ text }),
  })
  if (!response.ok) {
    throw new ApiError('Could not synthesize speech.', response.status)
  }
  const blob = await response.blob()
  return URL.createObjectURL(blob)
}
