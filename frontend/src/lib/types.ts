/**
 * Mirrors api/schemas.py -- one interface per Pydantic model. Kept in sync
 * by hand (no codegen); if a backend field is added/renamed, update here.
 */

export type AgentStatus =
  | 'pending'
  | 'sanitizing_input'
  | 'classifying_followup'
  | 'retrieving_schema'
  | 'generating'
  | 'reviewing'
  | 'validating'
  | 'estimating_cost'
  | 'executing'
  | 'succeeded'
  | 'failed'
  | 'needs_clarification'
  | 'rejected'
  | 'rate_limited'

export interface ConversationExchange {
  question: string
  sql: string | null
  tables: string[]
  status: AgentStatus
}

export interface AttemptRecord {
  attempt: number
  sql: string | null
  outcome: string
  error: string | null
  will_retry: boolean
}

export interface SchemaTable {
  table_name: string
  ddl: string
  similarity_score: number
}

export interface Citation {
  filename: string
  chunk_index: number
  page_number: number | null
  document_id: string
  has_pdf_bytes: boolean
}

export interface SourceAnswer {
  answer: string
  citations: Citation[]
  status: string
}

export interface MediaGenerationResult {
  answer: string
  status: string
  // Opaque id to fetch via GET /media/{media_id} -- never the provider's
  // raw CDN URL. null whenever status != 'succeeded'.
  media_id: string | null
  media_type: 'image' | 'video' | null
  model: string | null
}

export interface MediaSearchHit {
  // Opaque id to fetch via GET /media/library/{media_id} -- never a raw
  // filesystem path.
  media_id: string
  media_type: 'image' | 'video'
  caption: string
  timestamp_start: number | null
  timestamp_end: number | null
}

export interface MediaSearchResult {
  answer: string
  status: string
  hits: MediaSearchHit[]
}

/** Mirrors agent.orchestrator.state.AttachmentResult -- the "attachments"
 * source's contribution when the question had file(s) attached. */
export interface AttachmentResult {
  answer: string
  status: string
  used_attachment_ids: string[]
  vision_unavailable: boolean
}

/** Mirrors api.schemas.AttachmentOut -- one uploaded file's metadata/status. */
export interface AttachmentOut {
  attachment_id: string
  filename: string
  media_type: string
  size_bytes: number
  processing_status: string
  processing_error: string | null
}

export interface AttachmentErrorOut {
  code: string
  attachment_id: string | null
  filename: string
  message: string
}

/** Mirrors api.schemas.ImageResizePresetOut. */
export interface ImageResizePresetOut {
  name: string
  width: number
  height: number
}

/** Mirrors api.schemas.AttachmentCapabilitiesResponse (`GET
 * /attachments/capabilities`) -- what this deployment can actually do with
 * an attachment right now. The composer's image-action menu reads this to
 * enable/disable each action honestly instead of assuming vision/OCR/
 * editing are always available. */
export interface AttachmentCapabilities {
  enabled: boolean
  vision_input: boolean
  vision_model: string | null
  ocr: boolean
  image_resize: boolean
  image_text_removal: boolean
  image_text_removal_method: string | null
  native_pdf_input: boolean
  max_image_bytes: number
  max_document_bytes: number
  max_attachments_per_message: number
  max_total_attachment_bytes: number
  max_resize_dimension_px: number
  max_text_removal_regions: number
  supported_image_extensions: string[]
  supported_document_extensions: string[]
  resize_presets: ImageResizePresetOut[]
}

/** Mirrors api.schemas.TextRegionOut -- one recognized word from `POST
 * /attachments/{id}/extract-text`. */
export interface TextRegionOut {
  text: string
  left: number
  top: number
  width: number
  height: number
  confidence: number
  low_confidence: boolean
}

/** Mirrors api.schemas.OcrExtractResponse. `raw_text`/`cleaned_text` are
 * both exact OCR output -- never an LLM paraphrase, see that route's own
 * docstring. */
export interface OcrExtractResponse {
  attachment_id: string
  operation: string
  raw_text: string
  cleaned_text: string
  regions: TextRegionOut[]
  warnings: string[]
}

/** Mirrors api.schemas.TextLineRegionOut -- one OCR-proposed text line for
 * the "Remove text" workflow's confirm/adjust step. */
export interface TextLineRegionOut {
  region_id: number
  text: string
  left: number
  top: number
  width: number
  height: number
  confidence: number
}

export interface DetectTextRegionsResponse {
  attachment_id: string
  regions: TextLineRegionOut[]
}

/** One rectangle, in source-image pixel coordinates -- sent to `POST
 * /attachments/{id}/remove-text` (mirrors api.schemas.ImageRegionIn). */
export interface ImageRegionIn {
  left: number
  top: number
  width: number
  height: number
}

/** Mirrors api.schemas.ImageEditResultResponse -- the shared shape both
 * resize and remove-text return: a brand-new, separately downloadable
 * attachment, never the original mutated in place. */
export interface ImageEditResultResponse {
  attachment_id: string
  source_attachment_id: string
  operation: string
  image_data_url: string
  media_type: string
  width: number
  height: number
  original_width: number | null
  original_height: number | null
  size_bytes: number
  warnings: string[]
}

export type ImageResizeFit = 'contain' | 'cover' | 'stretch'
export type ImageResizeOutputFormat = 'png' | 'jpeg' | 'webp'

/** Mirrors api.schemas.ImageResizeRequest. */
export interface ImageResizeRequest {
  width?: number | null
  height?: number | null
  fit?: ImageResizeFit
  output_format?: ImageResizeOutputFormat | null
  quality?: number
}

export interface AttachmentUploadResponse {
  attachments: AttachmentOut[]
  errors: AttachmentErrorOut[]
}

export interface AskRequest {
  question: string
  conversation_history?: ConversationExchange[]
  enable_insight?: boolean
  session_id?: string | null
  // Server-side conversation id (identity.models.Conversation.id) -- only
  // meaningful for a locally-authenticated caller (see
  // api/chat_persistence.py). Omit on the first turn of a new conversation.
  conversation_id?: string | null
  // Ids of files attached to this question, from a prior
  // POST /attachments/upload response -- see useChatAttachments.ts.
  attachment_ids?: string[]
}

export interface AskResponse {
  session_id: string
  // Set only for a locally-authenticated caller whose turn was actually
  // persisted server-side -- null otherwise (OIDC/static-token/unauthenticated
  // caller, or a persistence failure, which never affects the rest of this
  // response). See docs/chat-history-architecture.md.
  conversation_id: string | null
  status: AgentStatus
  database: string | null
  sql: string | null
  result_columns: string[] | null
  result_rows: unknown[][] | null
  row_count: number | null
  retry_count: number
  attempt_history: AttemptRecord[]
  insight: string | null
  cost_notice: string | null
  low_confidence_notice: string | null
  rejection_reason: string | null
  rejection_message: string | null
  rate_limit_message: string | null
  clarification_message: string | null
  failure_explanation: string | null
  error_history: string[]
  sources_used: string[]
  synthesized_answer: string | null
  document_result: SourceAnswer | null
  policy_result: SourceAnswer | null
  web_result: SourceAnswer | null
  generation_result: MediaGenerationResult | null
  media_search_result: MediaSearchResult | null
  attachment_result: AttachmentResult | null
  query_plan: string[] | null
  schema_tables: SchemaTable[]
  followup_classification: 'standalone' | 'followup' | 'ambiguous' | null
  followup_resolved_against: ConversationExchange | null
  // Set when the multi-source router dropped a picked source because the
  // caller's account role lacks permission for it (e.g. media generation
  // needs analyst/admin) -- the question was answered from whatever
  // remained instead, which may not match what was actually asked.
  permission_denied_notice: string | null
}

export interface ExecuteRequest {
  sql: string
  database?: string | null
}

/** Mirrors api.schemas.ChartRecommendationOut -- a suggested starting chart
 * type + reason from agent.result_charting.recommend_chart. Only ever used
 * by frontend/src/lib/chartEngine.ts as a *seed* for the initial axis
 * selection; every chart type's actual validity is always independently
 * recomputed from the real result_columns/result_rows (see that module's
 * own docstring for why this suggestion is never trusted outright). */
export interface ChartRecommendation {
  chart_type: string
  reason: string
  x_column: string | null
  y_column: string | null
}

export interface ExecuteResponse {
  status: 'succeeded' | 'rejected' | 'failed'
  database: string
  normalized_sql: string | null
  result_columns: string[] | null
  result_rows: unknown[][] | null
  row_count: number | null
  duration_ms: number | null
  column_types: Record<string, string>
  chart_recommendation: ChartRecommendation | null
  truncated: boolean
  error: string | null
}

export interface GoldenExampleFeedbackRequest {
  question: string
  sql: string
  database: string
}

/** Backs POST /feedback/message -- a like/dislike + optional comment on
 * ANY answer (SQL, document/policy RAG, web search, media), unlike
 * GoldenExampleFeedbackRequest above which only ever covers a confirmed,
 * executed SQL result. See ResponseFeedbackWidget.tsx. */
export interface MessageFeedbackRequest {
  question: string
  answer: string
  rating: 'positive' | 'negative'
  sql?: string | null
  database?: string | null
  sources_used?: string[]
  comment?: string | null
  conversation_id?: string | null
}

export interface SchemaRefreshResult {
  database: string
  table_count: number
}

export interface SchemaRefreshResponse {
  databases: SchemaRefreshResult[]
}

export interface ComponentHealth {
  ok: boolean
  detail: string
}

export interface DatabaseHealth {
  name: string
  connection: ComponentHealth
  schema_index: ComponentHealth
}

export interface HealthResponse {
  status: 'ok' | 'degraded'
  databases: DatabaseHealth[]
  ollama: ComponentHealth
  voice_enabled: boolean
  media_search_enabled: boolean
  local_auth_enabled: boolean
  // Startup/operator diagnostic for chat-image vision support --
  // vision_model_available is null whenever vision_enabled is false
  // (nothing to check); when true, it's a real, live lookup against
  // Ollama's own pulled-model list, not just "a name is configured."
  vision_enabled: boolean
  vision_provider: 'ollama' | null
  vision_model: string | null
  vision_model_available: boolean | null
  ocr_enabled: boolean
}

export interface ColumnOut {
  name: string
  type: string
  nullable: boolean
  is_primary_key: boolean
}

export interface TableOut {
  database: string
  table_name: string
  columns: ColumnOut[]
}

export interface TablesResponse {
  tables: TableOut[]
}

export type Collection = 'documents' | 'policies'
export type SensitivityCategory = 'compensation' | 'disciplinary' | 'legal' | null

export interface DocumentOut {
  id: string
  filename: string
  collection: Collection
  sensitivity_category: string | null
  upload_date: string
  status: 'processing' | 'ready' | 'failed'
  chunk_count: number
  error_message: string | null
  has_pdf_bytes: boolean
}

export interface DocumentListResponse {
  documents: DocumentOut[]
}

export interface DocumentUploadResponse {
  document_id: string | null
  filename: string
  status: 'ready' | 'failed'
  chunk_count: number
  warnings: string[]
  error_message: string | null
}

export interface ApiErrorBody {
  detail: string
  correlation_id?: string
}

export interface TranscribeResponse {
  text: string
  corrected_text: string | null
  stt_duration_ms: number
}

// --- Local auth (identity/, api/identity_auth.py) -- mirrors identity/schemas.py ---

export interface LocalUser {
  id: string
  email: string
  username: string | null
  display_name: string | null
  // True only for an account created before display_name became mandatory
  // at sign-up -- drives the one-time "complete your profile" prompt.
  needs_profile_completion: boolean
  status: string
  is_email_verified: boolean
  roles: string[]
  created_at: string
  last_login_at: string | null
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: LocalUser
}

export interface MessageResponse {
  message: string
}

export interface AuthSessionOut {
  id: string
  device_name: string | null
  user_agent: string | null
  ip_address: string | null
  created_at: string
  last_used_at: string
  expires_at: string
  is_current: boolean
}

// --- Server-side chat history (identity/repositories/history.py,
// api/chat_history.py) -- mirrors identity/schemas.py's own new models.
// This is the *permanent, cross-device* store -- see
// docs/chat-history-architecture.md. Only reachable for a locally-
// authenticated user (local_auth_enabled + signed in via useLocalAuthStore).

export interface ServerConversation {
  id: string
  title: string | null
  feature_type: string
  status: string
  created_at: string
  updated_at: string
  last_message_at: string | null
  archived_at: string | null
}

export interface ConversationListResponse {
  conversations: ServerConversation[]
  total: number
  limit: number
  offset: number
}

export type MessageRole = 'user' | 'assistant'

export interface ServerMessage {
  id: string
  conversation_id: string
  role: MessageRole
  content: string
  sequence_number: number
  created_at: string
  status: string | null
  model_name: string | null
  error_code: string | null
  // An assistant row's own metadata (e.g. { sql: "..." }, set when the turn
  // produced SQL) -- lets a reloaded past turn show its SQL again, not just
  // the plain answer text. Always null for a user row.
  metadata: { sql?: string | null } | null
}

export interface MessageListResponse {
  messages: ServerMessage[]
  total_turns: number
  limit: number
  offset: number
}

export interface SearchHit {
  conversation_id: string
  title: string | null
  matched_in: 'title' | 'message'
  snippet: string
  message_id: string | null
  updated_at: string
}

export interface SearchResponse {
  results: SearchHit[]
  total: number
  limit: number
  offset: number
  query: string
}
