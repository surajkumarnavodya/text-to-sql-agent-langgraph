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

export interface AskRequest {
  question: string
  conversation_history?: ConversationExchange[]
  enable_insight?: boolean
  session_id?: string | null
  // Server-side conversation id (identity.models.Conversation.id) -- only
  // meaningful for a locally-authenticated caller (see
  // api/chat_persistence.py). Omit on the first turn of a new conversation.
  conversation_id?: string | null
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
  query_plan: string[] | null
  schema_tables: SchemaTable[]
  followup_classification: 'standalone' | 'followup' | 'ambiguous' | null
  followup_resolved_against: ConversationExchange | null
}

export interface ExecuteRequest {
  sql: string
  database?: string | null
}

export interface PlotlyFigure {
  data: Record<string, unknown>[]
  layout: Record<string, unknown>
}

export interface ExecuteResponse {
  status: 'succeeded' | 'rejected' | 'failed'
  database: string
  normalized_sql: string | null
  result_columns: string[] | null
  result_rows: unknown[][] | null
  row_count: number | null
  duration_ms: number | null
  chart: PlotlyFigure | null
  error: string | null
}

export interface GoldenExampleFeedbackRequest {
  question: string
  sql: string
  database: string
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
