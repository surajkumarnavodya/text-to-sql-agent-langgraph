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
  image_blur: boolean
  image_text_removal: boolean
  image_text_removal_method: string | null
  // Real, generative AI-guided editing (2026-09-27) -- true only when the
  // server has both the feature flag and a real provider credential
  // configured (see Settings.enable_image_editing's own docstring for why
  // this is a separate flag from plain media generation). The editor's
  // "AI-guided editing" panel must key every enabled/disabled state off
  // this, never assume it's always available.
  image_ai_editing: boolean
  image_ai_editing_provider: string | null
  native_pdf_input: boolean
  max_image_bytes: number
  max_document_bytes: number
  max_attachments_per_message: number
  max_total_attachment_bytes: number
  max_resize_dimension_px: number
  max_text_removal_regions: number
  max_ai_edit_prompt_length: number
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

/** Mirrors api.schemas.BlurRegionRequest -- deterministic, local Pillow
 * Gaussian blur over a painted mask region. Never a model call, and works
 * even when AI-guided editing is disabled. */
export interface BlurRegionRequest {
  mask_data_url: string
  radius?: number
}

/** Mirrors api.schemas.AiImageEditOperation -- the fixed allowlist of
 * generative operations this app actually offers. Deliberately excludes
 * anything resembling "remove watermark" -- see
 * media_gen.image_edit_provider's own module docstring for why. */
export type AiImageEditOperation =
  | 'remove_object'
  | 'replace_background'
  | 'replace_sky'
  | 'region_edit'
  | 'enhance'

/** Mirrors api.schemas.AiImageEditRequest -- `POST
 * /attachments/{id}/ai-edit`. `{attachment_id}` in the URL is always a
 * fresh, transient attachment holding the editor's *current* canvas export
 * (crop/rotate/local annotations already baked in), never the original
 * file -- see ImageEditor.tsx's own "always snapshot the current canvas"
 * contract. */
export interface AiImageEditRequest {
  operation: AiImageEditOperation
  prompt: string
  mask_data_url?: string | null
  idempotency_key?: string | null
}

/** Mirrors api.schemas.AiImageEditResponse. Deliberately never an HTTP
 * error for an ordinary provider failure -- `status: 'failed'` plus a safe
 * `error_message` is the normal "the edit didn't work" shape. */
export interface AiImageEditResponse {
  operation: string
  status: 'completed' | 'failed'
  source_attachment_id: string
  mask_provided: boolean
  attachment_id: string | null
  image_data_url: string | null
  media_type: string | null
  width: number | null
  height: number | null
  size_bytes: number | null
  provider: string | null
  model: string | null
  warnings: string[]
  error_code: string | null
  error_message: string | null
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
  // Ollama model id (from a prior GET /models response's models[].id) to
  // use for this question's generation/planning/review/insight calls.
  // Omit to use the server-configured default -- see settingsStore.ts's
  // selectedModel and HistorySettingsSection.tsx's "AI Model" picker.
  model?: string | null
}

/** Mirrors `agent.provenance.DataTruthLevel`. Every analytics panel renders
 * a claim's level visibly (see TruthLevelBadge.tsx) -- a DATABASE_FACT is a
 * computed value, an AI_INFERENCE is this platform's own suggestion, and a
 * CONFIRMED_BUSINESS_TRUTH is a human-approved definition. Never conflated. */
export type TruthLevel = 'database_fact' | 'ai_inference' | 'confirmed_business_truth'

export interface ProvenancedClaim {
  value: string
  level: TruthLevel
  grounded_in: string[]
  source: string | null
}

/** Mirrors api.schemas.AnalyticalIntentOut -- Prompt 11's classification. */
export interface AnalyticalIntent {
  intent:
    | 'lookup'
    | 'aggregation'
    | 'trend'
    | 'comparison'
    | 'ranking'
    | 'distribution'
    | 'segmentation'
    | 'funnel'
    | 'cohort'
    | 'forecast'
    | 'anomaly'
    | 'root_cause'
    | 'recommendation'
  confidence: number
  metric_candidates: string[]
  dimensions: string[]
  time_requirement: string | null
  comparison: string | null
  filters: string[]
  expected_result_shape: string | null
  ambiguity_flags: string[]
  truth_level: TruthLevel
}

/** Mirrors api.schemas.AnalyticalPlanOut -- Prompt 12's validated plan. */
export interface AnalyticalPlanMetric {
  name: string
  table: string | null
  column: string | null
  aggregation: string
  governed_metric_key: string | null
}

export interface AnalyticalPlanDimension {
  name: string
  table: string
  column: string
}

export interface AnalyticalPlanFilter {
  table: string
  column: string
  operator: string
  value: string
}

export interface AnalyticalPlan {
  metrics: AnalyticalPlanMetric[]
  dimensions: AnalyticalPlanDimension[]
  filters: AnalyticalPlanFilter[]
  time_range: { table: string; column: string; description: string } | null
  grain: string | null
  comparison: { kind: string; description: string } | null
  ranking: { order_by: string; direction: string; top_n: number | null; per_group: string[] } | null
  sort: { field: string; direction: string }[]
  limit: number | null
  required_operations: string[]
  truth_level: TruthLevel
}

/** Mirrors api.schemas.GoverningMetricOut -- a published, human-approved
 * metric definition the answer was grounded in (CONFIRMED_BUSINESS_TRUTH). */
export interface GoverningMetric {
  business_name: string
  approved_expression: string | null
  aggregation: string | null
  text: string
}

/** Mirrors analytics.models.AnalyticsResult -- the deterministic statistical
 * breakdown of one result (Prompt 13), plus its anomaly/recommendation
 * extensions. Only the fields the analytics panels actually read are typed
 * here; the backend's full shape is a superset. */
export interface GrowthPoint {
  period: string
  value: number
  change_percent_from_previous: number | null
}

export interface GrowthStat {
  label_column: string
  value_column: string
  points: GrowthPoint[]
  overall_change_percent: number | null
  direction: 'up' | 'down' | 'flat' | null
  missing_periods: string[]
  formula: string
}

export interface RankingEntry {
  label: string
  value: number
  rank: number
  share_percent: number | null
}

export interface RankingStat {
  label_column: string
  value_column: string
  entries: RankingEntry[]
  truncated: boolean
  formula: string
}

export interface AnomalySignal {
  method: 'threshold' | 'percent_change' | 'rolling_zscore' | 'iqr' | 'seasonal'
  baseline_value: number
  actual_value: number
  deviation: number
  threshold_used: number
  formula: string
}

export interface AnomalyPoint {
  period: string
  value: number
  signals: AnomalySignal[]
}

export interface ColumnSummaryStat {
  column: string
  count: number
  null_count: number
  is_numeric: boolean
  minimum: number | null
  maximum: number | null
  mean: number | null
}

export interface AnalyticsFinding {
  kind: string
  claim: ProvenancedClaim
  growth: GrowthStat | null
  ranking: RankingStat | null
  anomaly: AnomalyPoint | null
  column_summary: ColumnSummaryStat | null
}

export interface AnalyticsResult {
  row_count: number
  findings: AnalyticsFinding[]
  shape: 'scalar' | 'time_series' | 'categorical_aggregate' | 'multidimensional' | 'raw_table' | 'empty' | null
  engine_version: string
  insufficient_data_reasons: string[]
}

/** Mirrors api.schemas.ForecastResultOut (Prompt 16). Always ai_inference. */
export interface ForecastPoint {
  period: string
  forecast: number
  lower_bound: number | null
  upper_bound: number | null
  horizon_step: number
}

export interface ForecastEvaluation {
  holdout_size: number
  mae: number
  rmse: number
  mape: number | null
  formula: string
}

export interface ForecastResult {
  status: 'ok' | 'rejected'
  rejection_reasons: string[]
  horizon: number
  model: {
    model: string
    version: string
    training_window_start: string
    training_window_end: string
    training_point_count: number
    period_kind: string
    supports_interval: boolean
    confidence_level: number
  } | null
  points: ForecastPoint[]
  evaluation: ForecastEvaluation | null
  limitations: string[]
  truth_level: TruthLevel
  summary: string
  engine_version: string
}

/** Mirrors recommendation.models.Recommendation's model_dump(). `kind` of
 * 'next_question' is a suggested follow-up the user can ask in one click
 * (the "related questions" affordance); 'action' is a suggested next step. */
export interface Recommendation {
  kind: 'next_question' | 'action'
  claim: ProvenancedClaim
  rationale: string | null
  category: string | null
  evidence: ProvenancedClaim[]
  affected_entity: string | null
  action: string | null
  measurable_impact: string | null
  confidence: number | null
  rule_or_model: string | null
  limitations: string[]
  generated_at: string
  engine_version: string
}

export interface AskResponse {
  session_id: string
  // Set only for a locally-authenticated caller whose turn was actually
  // persisted server-side -- null otherwise (OIDC/static-token/unauthenticated
  // caller, or a persistence failure, which never affects the rest of this
  // response). See docs/chat-history-architecture.md.
  conversation_id: string | null
  // The persisted identity.models.AiOutput.id for this turn, set alongside
  // conversation_id above. Pass this back as ExecuteRequest.message_id when
  // confirming this turn's SQL, so the confirmed result gets attached to
  // this exact saved turn (see api/chat_persistence.py::persist_execute_result).
  message_id: string | null
  status: AgentStatus
  database: string | null
  // The Ollama model actually used for this question -- either
  // AskRequest.model (once validated) or the server default.
  model: string | null
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
  /** Server-side pipeline time for this question, ms -- persisted with the turn. */
  answer_duration_ms?: number | null
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
  // Prompt 30 -- analytics dashboard fields. See the type declarations above
  // and api/schemas.py's own field descriptions for each one's guarantees.
  analytical_result: AnalyticsResult | null
  forecast_result: ForecastResult | null
  recommendations: Recommendation[]
  analytical_intent: AnalyticalIntent | null
  analytical_plan: AnalyticalPlan | null
  governing_metrics: GoverningMetric[]
  restricted_field_notice: string | null
}

export interface ExecuteRequest {
  sql: string
  database?: string | null
  // AskResponse.conversation_id/message_id from the /ask call this SQL came
  // from, if any -- when supplied (a locally-authenticated caller), a
  // successful execution's result is attached to that already-persisted
  // turn so reopening the conversation later shows it immediately, without
  // re-running anything. See api/chat_persistence.py::persist_execute_result.
  conversation_id?: string | null
  message_id?: string | null
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
  /** Prompt 30 -- the JSON mirror of the X-Cache header. 'hit' means this
   * result was served from the server's short-TTL result cache (no database
   * round trip). Null when the cache is disabled or this SQL isn't cacheable. */
  cache_status: 'hit' | 'miss' | null
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
  /** Tenant-admin route only: set when this one database's refresh failed. */
  error?: string | null
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
  /** Whether "Anyone with the link" sharing is allowed on this server. */
  share_public_links_enabled?: boolean
  voice_enabled: boolean
  media_search_enabled: boolean
  local_auth_enabled: boolean
  // Google sign-in capability discovery (2026-09-28) -- google_client_id is
  // a PUBLIC OAuth client ID, never a secret (see security/google_oidc.py's
  // own module docstring for why this flow never holds a client secret at
  // all), and is only ever non-null alongside google_signin_enabled=true.
  google_signin_enabled: boolean
  google_client_id: string | null
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

/** One selectable Ollama model, as GET /models reports it -- mirrors
 * api.schemas.ModelOut / agent.model_registry.ModelOption. `installed`/
 * `available` are a live lookup against the *connected* Ollama instance
 * (never the online Ollama Library) -- a model can be `enabled` (configured)
 * but not `installed` (never pulled on this machine); the picker must
 * disable selecting it in that state. */
export interface ModelOut {
  id: string
  display_name: string
  is_default: boolean
  enabled: boolean
  installed: boolean
  available: boolean
  recommended: boolean
  parameter_size: string
  context_length: number | null
  resource_level: string
  capabilities: string[]
  description: string
}

export interface ModelsResponse {
  provider: 'ollama'
  default_model: string
  selection_enabled: boolean
  models: ModelOut[]
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

// --- Google sign-in (2026-09-28, security/google_oidc.py, api/identity_auth.py) ---

export interface GoogleNonceResponse {
  nonce: string
}

export interface LinkedIdentityOut {
  provider: string
  email_at_link: string | null
  created_at: string
  last_used_at: string
}

export interface LinkedIdentityListResponse {
  identities: LinkedIdentityOut[]
  /** Whether removing a linked identity is even possible right now --
   * the settings page disables "unlink" instead of letting the user find
   * out via a failed request when this is false. */
  has_password: boolean
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

/** One persisted attachment reference, taken from the process-lifetime
 * `AttachmentStore` at save time -- see api/chat_persistence.py's
 * `_attachment_refs_snapshot`. If the attachment has since been evicted
 * from that in-memory store, `filename` falls back to the bare id and
 * `media_type` to a generic value -- there is no bytes/preview to restore
 * either way, by design (attachments are ephemeral, never persisted to
 * disk-plus-database forever). */
export interface PersistedAttachmentRef {
  attachment_id: string
  filename: string
  media_type: string
}

/** The bounded, confirmed-execution snapshot persisted once a "Confirm and
 * Run" succeeds for a turn whose message_id was known (see
 * api/chat_persistence.py::persist_execute_result) -- lets a reopened
 * conversation show a previously confirmed result immediately, without
 * re-running the SQL. `rows`/`returned_rows` may be fewer than `row_count`
 * if the live result exceeded Settings.chat_history_max_result_rows --
 * `truncated` covers both that and the live execution's own truncation. */
export interface PersistedResultSnapshot {
  columns: string[]
  rows: unknown[][]
  row_count: number | null
  returned_rows: number
  truncated: boolean
  column_types: Record<string, string>
  chart_recommendation: ChartRecommendation | null
  normalized_sql: string | null
  duration_ms: number | null
  captured_at: string
}

/** Mirrors `api.chat_persistence._build_history_metadata`'s output --
 * the full, versioned snapshot stored in `ai_outputs.metadata` for one
 * assistant turn. Every field is optional/nullable: a record created
 * before `schema_version` existed (the old `{ sql: "..." }`-or-nothing
 * shape) has none of the richer fields, which
 * `frontend/src/lib/history.ts::serverMessagesToQueryHistory` must degrade
 * gracefully against rather than assume. */
export interface ServerMessageMetadata {
  schema_version?: number
  answer_duration_ms?: number | null
  status?: AgentStatus
  sources_used?: string[]
  database?: string | null
  model?: string | null
  sql?: string | null
  row_count?: number | null
  retry_count?: number
  query_plan?: string[] | null
  schema_tables?: { table_name: string; similarity_score: number }[]
  insight?: string | null
  synthesized_answer?: string | null
  cost_notice?: string | null
  low_confidence_notice?: string | null
  rejection_reason?: string | null
  rejection_message?: string | null
  rate_limit_message?: string | null
  clarification_message?: string | null
  failure_explanation?: string | null
  permission_denied_notice?: string | null
  document_result?: SourceAnswer | null
  policy_result?: SourceAnswer | null
  web_result?: SourceAnswer | null
  generation_result?: MediaGenerationResult | null
  media_search_result?: MediaSearchResult | null
  attachment_result?: AttachmentResult | null
  attachment_refs?: PersistedAttachmentRef[]
  result_snapshot?: PersistedResultSnapshot | null
}

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
  // An assistant row's own persisted metadata -- see ServerMessageMetadata's
  // own docstring. Always null for a user row.
  metadata: ServerMessageMetadata | null
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

// --- Secure conversation sharing (api/shares.py) --------------------------
// See identity/share_schemas.py's own docstring: no response shape here
// ever carries a raw share-link token except ShareLinkOut, and only from
// the two owner actions explicitly authorized to mint one.

export type ShareAccessMode = 'invite_only' | 'anyone_with_link'
export type ShareStatus = 'active' | 'disabled'
export type ShareMemberStatus = 'pending' | 'active' | 'revoked' | 'expired'

export interface ShareMember {
  id: string
  user_id: string | null
  invited_email: string | null
  display_name: string | null
  role: 'viewer'
  status: ShareMemberStatus
  expires_at: string | null
  accepted_at: string | null
  revoked_at: string | null
  created_at: string
}

export interface Share {
  id: string
  conversation_id: string
  access_mode: ShareAccessMode
  default_permission: 'viewer'
  status: ShareStatus
  snapshot_message_sequence: number
  snapshot_captured_at: string | null
  expires_at: string | null
  revoked_at: string | null
  /** Optimistic-concurrency token -- every PATCH/revoke must echo back
   * whatever this was on the copy the caller most recently read. */
  version: number
  created_at: string
  updated_at: string
  members: ShareMember[]
  link_available: boolean
  member_view_path: string
}

export interface ShareLink {
  /** A relative path (e.g. "/shared/<raw-token>") -- the caller prepends
   * `window.location.origin` itself; this server never constructs an
   * absolute URL (see ShareLinkOut's own docstring for why). */
  view_path: string
  expires_at: string | null
}

export interface ShareResponse {
  share: Share
  link: ShareLink | null
}

export interface CreateShareRequest {
  access_mode?: ShareAccessMode
  expiry_days?: number | null
}

export interface UpdateShareRequest {
  version: number
  access_mode?: ShareAccessMode | null
  expiry_days?: number | null
  status?: ShareStatus | null
  refresh_snapshot?: boolean
}

export interface InviteMemberRequest {
  email: string
  expiry_days?: number | null
}

export interface InviteMemberResponse {
  member: ShareMember
  invitation_path: string | null
}

export interface AcceptInvitationResponse {
  conversation_id: string
  member_view_path: string
}

export interface ProjectedTurn {
  sequence_number: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
  sources_used: string[]
  database: string | null
  model: string | null
  sql: string | null
  row_count: number | null
  insight: string | null
  synthesized_answer: string | null
  document_result: Record<string, unknown> | null
  policy_result: Record<string, unknown> | null
  web_result: Record<string, unknown> | null
  generation_result: Record<string, unknown> | null
  media_search_result: Record<string, unknown> | null
  attachment_refs: { attachment_id: string; filename: string; media_type: string }[]
  result_snapshot: Record<string, unknown> | null
}

export interface SharedConversation {
  conversation_title: string | null
  feature_type: string
  snapshot_captured_at: string | null
  viewer_role: 'owner' | 'member' | 'public_link'
  turns: ProjectedTurn[]
}

// --- Client-database onboarding (onboarding/, api/onboarding.py) -- mirrors
// api/onboarding_schemas.py exactly. `db_password` is request-only on the
// three request types below; it is never a field on `OnboardingJob` --
// the backend never persists it (see identity/models.py's own docstring),
// so there is nothing for this UI to accidentally display back. ---

export type OnboardingJobStatus =
  | 'pending'
  | 'discovering'
  | 'awaiting_review'
  | 'publishing'
  | 'published'
  | 'failed'
  | 'cancelled'

export type OnboardingReviewItemType =
  | 'pii_classification'
  | 'relationship'
  | 'semantic_label'
  | 'golden_question'

export type OnboardingReviewDecision = 'pending' | 'confirmed' | 'rejected'

export interface CreateOnboardingJobRequest {
  database_label: string
  db_type: string
  db_host?: string | null
  db_port?: number | null
  db_name?: string | null
  db_user?: string | null
  db_password?: string | null
  db_schema?: string | null
}

export interface RunDiscoveryRequest {
  db_password?: string | null
  verify_relationships_with_data?: boolean
  verify_pii_with_data?: boolean
}

export interface PublishJobRequest {
  db_password?: string | null
}

export interface DecideReviewItemRequest {
  decision: 'confirmed' | 'rejected'
  notes?: string | null
}

export interface OnboardingJob {
  id: string
  database_label: string
  db_type: string
  db_host: string | null
  db_port: number | null
  db_name: string | null
  db_user: string | null
  db_schema: string | null
  status: OnboardingJobStatus
  current_stage: string | null
  error_message: string | null
  retry_count: number
  discovery_summary: Record<string, unknown> | null
  version: number
  created_at: string
  updated_at: string
}

export interface OnboardingReviewItem {
  id: string
  item_type: OnboardingReviewItemType
  table_name: string | null
  column_name: string | null
  subject: string
  payload: Record<string, unknown>
  confidence: number
  is_ambiguous: boolean
  decision: OnboardingReviewDecision
  decided_at: string | null
  decision_notes: string | null
  created_at: string
}

export interface OnboardingArtifact {
  id: string
  artifact_type: 'semantic_contract' | 'golden_questions' | 'evaluation_report'
  content: Record<string, unknown>
  version: number
  created_at: string
}

// --- Tenant-aware semantic catalog (semantic/catalog.py, api/semantic_catalog.py)
// -- mirrors api/semantic_catalog_schemas.py exactly. Prompt 09/10, with
// `reviewed_by_display_name`/`published_by_display_name`/
// `conflicting_entry_ids`/`conflicting_entry_names` surfaced for the first
// time by the SME review dashboard (Prompt 27). ---

export type CatalogConceptType = 'entity' | 'metric' | 'dimension' | 'domain'
export type CatalogStatus = 'draft' | 'reviewed' | 'published' | 'superseded'

export interface CreateCatalogEntryRequest {
  database_id: string
  concept_type: CatalogConceptType
  concept_key: string
  business_name: string
  technical_name?: string | null
  description?: string
  grain?: string | null
  keys?: string[]
  relationships?: Record<string, unknown>[]
  domain?: string | null
  synonyms?: string[]
  business_rules?: string[]
  examples?: string[]
  evidence?: Record<string, unknown>[]
  confidence?: number
  owner?: string | null
  approved_expression?: string | null
  source_tables?: string[]
  filters?: string[]
  dimensions?: string[]
  aggregation?: string | null
}

export type UpdateCatalogEntryRequest = Partial<
  Omit<CreateCatalogEntryRequest, 'database_id' | 'concept_type' | 'concept_key'>
>

export interface ReviewDecisionRequest {
  notes?: string | null
}

export interface CatalogEntryOut {
  id: string
  tenant_id: string
  database_id: string
  concept_type: CatalogConceptType
  concept_key: string
  business_name: string
  technical_name: string | null
  description: string
  grain: string | null
  keys: string[]
  relationships: Record<string, unknown>[]
  domain: string | null
  synonyms: string[]
  business_rules: string[]
  examples: string[]
  evidence: Record<string, unknown>[]
  confidence: number
  status: CatalogStatus
  owner: string | null
  version: number
  supersedes_id: string | null
  truth_level: string
  reviewed_at: string | null
  review_notes: string | null
  reviewed_by_user_id: string | null
  reviewed_by_display_name: string | null
  published_by_user_id: string | null
  published_by_display_name: string | null
  published_at: string | null
  created_at: string
  updated_at: string
  approved_expression: string | null
  source_tables: string[]
  filters: string[]
  dimensions: string[]
  aggregation: string | null
  conflicting_entry_ids: string[]
  conflicting_entry_names: string[]
}

// --- Global platform admin dashboard (api/platform_admin.py, Prompt 28) --
// mirrors api/platform_admin_schemas.py exactly. Every route here is
// gated on identity.rbac.Permission.PLATFORM_ADMIN, a genuinely different
// dimension from the tenant-scoped "admin" role every other admin surface
// in this app uses. ---

export type TenantStatus = 'active' | 'suspended'
export type AuditOutcome = 'success' | 'failure' | 'denied'
export type SecuritySeverity = 'info' | 'warning' | 'critical'

export interface CreateTenantRequest {
  tenant_id: string
  name: string
}

export interface SetTenantStatusRequest {
  status: TenantStatus
}

export interface AssignRoleRequest {
  role_name: string
}

export interface TenantOut {
  id: string
  name: string
  status: TenantStatus
  user_count: number
  created_at: string
  updated_at: string
}

export interface PlatformUserOut {
  id: string
  email: string
  display_name: string | null
  tenant_id: string
  status: string
  roles: string[]
  created_at: string
  last_login_at: string | null
}

export interface RoleOut {
  name: string
  description: string | null
  is_system_role: boolean
  permissions: string[]
}

export interface DatabaseStatusOut {
  name: string
  db_type: string
  db_host: string | null
  db_port: number | null
  db_name: string | null
  db_schema: string | null
  tenant_ids: string[]
  healthy: boolean
  detail: string
}

export interface SemanticReviewQueueOut {
  catalog_draft_count: number
  catalog_reviewed_count: number
  catalog_published_count: number
  onboarding_pending_by_type: Record<string, number>
  total_pending: number
}

export interface PlatformOnboardingJobOut {
  id: string
  tenant_id: string
  database_label: string
  status: string
  created_at: string
  updated_at: string
}

export interface SecurityEventOut {
  timestamp: string
  event_type: string
  severity: SecuritySeverity
  detail: string
  correlation_id: string | null
  tenant_id: string | null
  context: Record<string, string>
}

export interface AuditLogOut {
  id: string
  actor_user_id: string | null
  subject_user_id: string | null
  action: string
  resource_type: string
  resource_id: string | null
  outcome: AuditOutcome
  created_at: string
  metadata: Record<string, unknown> | null
}

export interface ConfigStatusOut {
  flags: Record<string, boolean>
}

// Mirrors api/schemas.py's StageMetricOut/RequestMetricOut/
// PerformanceMetricsResponse -- no frontend consumer existed before
// Prompt 28, since GET /metrics/performance was deliberately
// operator-only (see observability/metrics.py's own docstring);
// GET /platform-admin/metrics is the first route this type serves.

export interface StageMetricOut {
  stage: string
  count: number
  mean_ms: number
  p50_ms: number
  p95_ms: number
  max_ms: number
  total_ms: number
}

export interface RequestMetricOut {
  count: number
  mean_ms: number
  p50_ms: number
  p95_ms: number
  max_ms: number
}

export interface PerformanceMetricsResponse {
  started_at: string
  window_requests: number
  max_window_requests: number
  requests: RequestMetricOut
  stages: StageMetricOut[]
  status_counts: Record<string, number>
  tenant_id: string | null
  result_cache_hits: number
  result_cache_misses: number
  database_concurrency_rejections: number
}

// --- Tenant/client admin dashboard (api/tenant_admin.py, Prompt 29) --
// mirrors api/tenant_admin_schemas.py's own new shapes. Every other type
// this dashboard needs (TenantOut, PlatformUserOut, RoleOut,
// DatabaseStatusOut, SecurityEventOut, AssignRoleRequest) is the exact
// same type the platform-admin dashboard already defined above, reused
// as-is -- only the query scope differs, enforced server-side. ---

export interface SemanticCatalogStatusOut {
  draft_count: number
  reviewed_count: number
  published_count: number
  superseded_count: number
}

export interface PendingReviewsOut {
  onboarding_pending_by_type: Record<string, number>
  catalog_pending_count: number
  total_pending: number
}

export interface GoldenQuestionsSummaryOut {
  job_id: string
  database_label: string
  question_count: number
}

export interface EvaluationSummaryOut {
  job_id: string
  database_label: string
  pass_count: number
  total_count: number
  evaluated_at: string
}

// --- Recommendation governance (api/recommendation_governance.py,
// Prompt 17/18) -- the first frontend consumer of this API; mirrors
// api/recommendation_governance_schemas.py. Reused read-only by this
// dashboard (list + quality metrics only); the fuller feedback/resolve/
// expire governance workflow is a disclosed, deliberately deferred
// follow-up, matching this codebase's own "compute + test, defer
// deepest UI wiring" precedent already used elsewhere (e.g. Prompt 15's
// visualization_spec). ---

export type RecommendationStatus =
  | 'generated'
  | 'reviewed'
  | 'accepted'
  | 'rejected'
  | 'partially_useful'
  | 'incorrect'
  | 'resolved'
  | 'expired'

/** One supporting claim behind a recommendation -- a serialized
 * `agent.provenance.ProvenancedClaim`. Evidence is only ever `database_fact`
 * or `confirmed_business_truth` (`recommendation.models.Recommendation`
 * enforces that), so a reviewer can always tell what the claim rests on. */
export interface RecommendationEvidenceItem {
  value: string
  level: TruthLevel
  grounded_in: string[]
  source: string | null
}

export type RecommendationCategory =
  | 'performance'
  | 'anomaly'
  | 'revenue'
  | 'customer'
  | 'product'
  | 'operations'
  | 'data_quality'
  | 'security'
  | 'database_performance'

/** The verdicts `POST /recommendations/{id}/feedback` accepts. `resolved`
 * and `expired` each have their own route (`.../resolve`, `.../expire`). */
export type RecommendationVerdict =
  | 'reviewed'
  | 'accepted'
  | 'rejected'
  | 'partially_useful'
  | 'incorrect'

export interface RecommendationRecordOut {
  id: string
  tenant_id: string
  database_id: string
  category: RecommendationCategory | string | null
  kind: string
  rule_or_model: string | null
  claim_text: string
  rationale: string | null
  affected_entity: string | null
  action: string | null
  measurable_impact: string | null
  confidence: number | null
  evidence: RecommendationEvidenceItem[]
  limitations: string[]
  engine_version: string
  evidence_version: string
  status: RecommendationStatus
  generated_at: string
  source_question: string | null
  source_sql: string | null
  created_at: string
  updated_at: string
  /** Prompt 31 -- `null` when unassigned, or when the owner has no display
   * name set (never an email address). */
  owner_user_id?: string | null
  owner_display_name?: string | null
}

/** Prompt 32 -- one screen this caller may open (`security/navigation.py`). */
export interface NavItemOut {
  id: string
  path: string
  group: 'workspace' | 'review' | 'administration'
}

/** Prompt 32 -- the server's decision for the signed-in caller: which screens
 * they may open, and which named actions they may offer. */
export interface NavigationOut {
  items: NavItemOut[]
  capabilities: Record<string, boolean>
  roles: string[]
  tenant_id: string | null
}

export type RecommendationEventType = 'status_change' | 'note' | 'owner_assigned'

export interface RecommendationFeedbackEventOut {
  id: string
  recommendation_id: string
  from_status: RecommendationStatus | null
  to_status: RecommendationStatus
  event_type?: RecommendationEventType
  actor_user_id: string | null
  actor_label: string | null
  reason: string | null
  detail?: { owner_user_id: string | null; previous_owner_user_id: string | null } | null
  recommendation_version: string
  evidence_version: string
  created_at: string
}

export interface RecommendationQualityMetricsOut {
  total: number
  by_status: Record<string, number>
  by_category: Record<string, Record<string, number>>
  judged_total: number
  acceptance_rate: number | null
}
