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

export interface AskRequest {
  question: string
  conversation_history?: ConversationExchange[]
  enable_insight?: boolean
  session_id?: string | null
}

export interface AskResponse {
  session_id: string
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
