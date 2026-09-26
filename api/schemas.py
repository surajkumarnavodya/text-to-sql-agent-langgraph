"""Pydantic request/response models for `api/main.py`.

Deliberately thin: these mirror the subset of `agent.state.AgentState` the
React dashboard renders, not a new data model. Nothing here re-decides
what's safe to return -- the agent graph itself is what already gates what
ends up in `AgentState` (validated SQL only, row-capped results, redacted
errors).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.state import AgentStatus


class ConversationExchangeIn(BaseModel):
    """One prior turn, for follow-up reference resolution -- same shape as
    `agent.state.ConversationExchange`, accepted from an API caller instead
    of being built from server-side session state (the API has no
    server-side session of its own; the caller is responsible for
    resending recent turns each request)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    question: str = Field(..., min_length=1)
    sql: str | None = None
    tables: list[str] = Field(default_factory=list)
    status: AgentStatus = "succeeded"


class AskRequest(BaseModel):
    """The upper bound on `question`'s length is deliberately NOT duplicated
    here as a `Field(max_length=...)` -- it's config-driven
    (`Settings.max_question_length`, default 500, overridable via `.env`)
    and already enforced downstream by `agent.input_guard.check_input`
    (the "too_long" `RejectionReason`), which is the single source of
    truth for it. A static schema-level cap would either hardcode a wrong
    number or drift from that setting the moment someone changes it."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    question: str = Field(..., min_length=1, description="Natural-language question.")
    conversation_history: list[ConversationExchangeIn] = Field(
        default_factory=list,
        description="Recent prior turns (oldest first), for follow-up resolution. Optional.",
    )
    enable_insight: bool = Field(
        default=True,
        description="Whether to attempt a plain-English insight sentence after execution.",
    )
    session_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Caller-supplied conversation identifier. Omit on the first turn of a "
            "conversation -- the API generates one and returns it in AskResponse. "
            "Pass the same value back on every subsequent turn of that conversation. "
            "Purely a correlation token today (the API is still stateless -- see "
            "ConversationExchangeIn's docstring); it does not yet gate or scope "
            "anything server-side, but is the identifier future server-side "
            "conversation state (e.g. the multi-source router) will key off of."
        ),
    )
    conversation_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Server-side conversation id (identity.models.Conversation.id) to persist this "
            "turn under -- only meaningful for a locally-authenticated caller (see "
            "api/chat_persistence.py); ignored otherwise. Omit on the first turn of a new "
            "conversation -- one is created automatically and returned in AskResponse. "
            "Unrelated to session_id above: this is real, permanent server-side storage, "
            "not a correlation token."
        ),
    )
    attachment_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Ids of files the caller attached to this question, from a prior "
            "POST /attachments/upload response. Each is resolved server-side (scoped to "
            "this caller -- see attachments/store.py's ownership check) and given to the "
            "model as extra context/multimodal content via the 'attachments' orchestrator "
            "source (see agent/orchestrator/nodes.py::attachment_node). Works even without "
            "ENABLE_MULTI_SOURCE_ROUTER=true -- see agent.orchestrator.graph.run_orchestrated's "
            "docstring."
        ),
    )


class AttemptRecordOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt: int
    sql: str | None = None
    outcome: str
    error: str | None = None
    will_retry: bool = False


class SchemaTableOut(BaseModel):
    """One retrieved-schema table entry -- mirrors `agent.state.TableSchema`,
    used by the dashboard's "Retrieved schema context" expander."""

    model_config = ConfigDict(frozen=True)

    table_name: str
    ddl: str
    similarity_score: float


class CitationOut(BaseModel):
    """Mirrors `rag.graph.Citation` -- one cited chunk's provenance, enough
    for a client to show "Sources: ..." text and, when `has_pdf_bytes` is
    true, fetch the original PDF via `GET /documents/{document_id}/download`."""

    model_config = ConfigDict(frozen=True)

    filename: str
    chunk_index: int
    page_number: int | None = None
    document_id: str
    has_pdf_bytes: bool


class SourceAnswerOut(BaseModel):
    """Mirrors `agent.orchestrator.state.SourceAnswer` -- one non-SQL
    source's contribution (documents/policy/web) to a multi-source answer."""

    model_config = ConfigDict(frozen=True)

    answer: str
    citations: list[CitationOut] = Field(default_factory=list)
    status: str


class MediaGenerationResultOut(BaseModel):
    """Mirrors `agent.orchestrator.state.MediaGenerationResult` -- the
    "generation" source's contribution. `media_id` is an opaque id to fetch
    the actual bytes via `GET /media/{media_id}` (`api/media.py`) --
    deliberately never the provider's raw CDN URL, so nothing the client
    ever sees can go stale/expire on IMA's side or bypass this app's own
    control over what gets served. `media_id`/`media_type`/`model` are None
    whenever `status != "succeeded"` (a rejected/rate-limited request, a
    provider failure, or media generation simply being disabled --
    see `Settings.enable_media_generation`'s docstring)."""

    model_config = ConfigDict(frozen=True)

    answer: str
    status: str
    media_id: str | None = None
    media_type: str | None = None
    model: str | None = None


class MediaSearchHitOut(BaseModel):
    """Mirrors `agent.orchestrator.state.MediaSearchHit` -- one retrieved
    image or video segment. `media_id` is an opaque id to fetch the actual
    bytes via `GET /media/library/{media_id}` (`api/media_library.py`) --
    never a raw filesystem path. No similarity score -- that stays an
    internal ranking detail, never surfaced to a client."""

    model_config = ConfigDict(frozen=True)

    media_id: str
    media_type: str
    caption: str
    timestamp_start: float | None = None
    timestamp_end: float | None = None


class AttachmentErrorOut(BaseModel):
    """Mirrors `attachments.models.AttachmentError` -- one structured,
    user-friendly upload/processing failure. `code` is stable (an
    `AttachmentErrorCode` value) so a client can branch on it rather than
    string-matching `message`."""

    model_config = ConfigDict(frozen=True)

    code: str
    attachment_id: str | None = None
    filename: str
    message: str


class AttachmentOut(BaseModel):
    """Mirrors the client-relevant subset of `attachments.models.Attachment`
    -- backs `POST /attachments/upload`'s per-file response entry and the
    composer's attachment card (filename/type/size/status/error). Never
    includes `image_data_url`/`extracted_text` -- those can be large and are
    only ever consumed server-side (`attachments.pipeline
    .to_processed_attachment`); the client already has its own local object-
    URL preview for an image (see `frontend/src/hooks/useImageAttachments.ts`),
    so there's no reason to round-trip the same bytes back down re-encoded."""

    model_config = ConfigDict(frozen=True)

    attachment_id: str
    filename: str
    media_type: str
    size_bytes: int
    processing_status: str
    processing_error: str | None = None


class AttachmentUploadResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    attachments: list[AttachmentOut] = Field(default_factory=list)
    errors: list[AttachmentErrorOut] = Field(default_factory=list)


class ImageResizePresetOut(BaseModel):
    """Mirrors `attachments.image_ops.RESIZE_PRESETS`'s entries."""

    model_config = ConfigDict(frozen=True)

    name: str
    width: int
    height: int


class AttachmentCapabilitiesResponse(BaseModel):
    """Mirrors `attachments.capabilities.AttachmentCapabilitiesOut` -- what
    `GET /attachments/capabilities` returns. The frontend uses this to
    enable/disable each attachment action truthfully rather than assuming
    vision/OCR/editing are always available (per this feature's own "do not
    display a capability as working unless it actually is" requirement)."""

    model_config = ConfigDict(frozen=True)

    enabled: bool
    vision_input: bool
    vision_model: str | None = None
    ocr: bool
    image_resize: bool
    image_text_removal: bool
    image_text_removal_method: str | None = None
    native_pdf_input: bool = False
    max_image_bytes: int
    max_document_bytes: int
    max_attachments_per_message: int
    max_total_attachment_bytes: int
    max_resize_dimension_px: int
    max_text_removal_regions: int
    supported_image_extensions: list[str] = Field(default_factory=list)
    supported_document_extensions: list[str] = Field(default_factory=list)
    resize_presets: list[ImageResizePresetOut] = Field(default_factory=list)


class TextRegionOut(BaseModel):
    """Mirrors `attachments.ocr_extract.TextRegion` -- one recognized word
    and its bounding box, for `POST /attachments/{id}/extract-text`."""

    model_config = ConfigDict(frozen=True)

    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float
    low_confidence: bool


class OcrExtractResponse(BaseModel):
    """Mirrors `attachments.ocr_extract.OcrExtractionResult`. Deliberately
    keeps `raw_text` and `cleaned_text` as two separate fields -- see that
    module's own docstring for why an OCR action must never silently
    substitute an LLM paraphrase for the actual recognized text."""

    model_config = ConfigDict(frozen=True)

    attachment_id: str
    operation: str = "extract_text"
    raw_text: str
    cleaned_text: str
    regions: list[TextRegionOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TextLineRegionOut(BaseModel):
    """Mirrors `attachments.inpaint.TextLineRegion` -- one OCR-proposed
    text line, for the "Remove text" workflow's confirm/adjust step."""

    model_config = ConfigDict(frozen=True)

    region_id: int
    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float


class DetectTextRegionsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    attachment_id: str
    regions: list[TextLineRegionOut] = Field(default_factory=list)


class ImageRegionIn(BaseModel):
    """One rectangle, in source-image pixel coordinates -- either copied
    from a `TextLineRegionOut` the caller confirmed, or drawn manually."""

    model_config = ConfigDict(extra="forbid")

    left: int = Field(ge=0)
    top: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class RemoveTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regions: list[ImageRegionIn] = Field(min_length=1)


class ImageEditResultResponse(BaseModel):
    """Shared response shape for both `POST /attachments/{id}/resize` and
    `POST /attachments/{id}/remove-text` -- both produce a brand-new,
    separately stored/downloadable image attachment (the original is never
    mutated), so both return the same fields."""

    model_config = ConfigDict(frozen=True)

    attachment_id: str
    source_attachment_id: str
    operation: str
    image_data_url: str
    media_type: str
    width: int
    height: int
    original_width: int | None = None
    original_height: int | None = None
    size_bytes: int
    warnings: list[str] = Field(default_factory=list)


class ImageResizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fit: Literal["contain", "cover", "stretch"] = "contain"
    output_format: Literal["png", "jpeg", "webp"] | None = None
    quality: int = Field(default=90, ge=1, le=100)


class AttachmentResultOut(BaseModel):
    """Mirrors `agent.orchestrator.state.AttachmentResult` -- the
    "attachments" source's contribution to an `/ask` answer."""

    model_config = ConfigDict(frozen=True)

    answer: str
    status: str
    used_attachment_ids: list[str] = Field(default_factory=list)
    vision_unavailable: bool = False


class MediaSearchResultOut(BaseModel):
    """Mirrors `agent.orchestrator.state.MediaSearchResult` -- the
    "media_search" source's contribution."""

    model_config = ConfigDict(frozen=True)

    answer: str
    status: str
    hits: list[MediaSearchHitOut] = Field(default_factory=list)


class MediaSearchRequest(BaseModel):
    """`POST /search/media` -- direct media search, independent of the
    conversational `/ask` flow."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., min_length=1)
    media_type: Literal["image", "video", "any"] = "any"


class ConversationExchangeOut(BaseModel):
    """Mirrors `agent.state.ConversationExchange` -- the specific prior turn
    a "followup" question was resolved against, for a client to render a
    "Following up on: ..." caption."""

    model_config = ConfigDict(frozen=True)

    question: str
    sql: str | None = None
    tables: list[str] = Field(default_factory=list)
    status: AgentStatus


class AskResponse(BaseModel):
    """Mirrors the fields of `agent.state.AgentState` that the React
    dashboard surfaces to a human -- see its `TurnCard.tsx` for the
    reference rendering this response shape is kept consistent with."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(
        description=(
            "Echoes AskRequest.session_id, or a freshly generated one if the "
            "caller didn't supply one -- pass this back on the next request "
            "in the same conversation."
        )
    )
    conversation_id: str | None = Field(
        default=None,
        description=(
            "The server-side conversation this turn was persisted under -- set only for a "
            "locally-authenticated caller (see api/chat_persistence.py); null for an "
            "OIDC/static-token/unauthenticated caller, or if persistence itself failed "
            "(never a reason this response's own status/sql/result fields are affected). "
            "Pass this back as AskRequest.conversation_id on the next turn of the same "
            "conversation."
        ),
    )
    status: AgentStatus
    database: str | None = Field(
        default=None,
        description=(
            "Which configured database (Settings.databases[i].name) this question "
            "was auto-routed to. 'default' for a plain single-database setup."
        ),
    )
    sql: str | None = None
    result_columns: list[str] | None = None
    result_rows: list[list[Any]] | None = None
    row_count: int | None = None
    retry_count: int = 0
    attempt_history: list[AttemptRecordOut] = Field(default_factory=list)
    insight: str | None = None
    cost_notice: str | None = None
    low_confidence_notice: str | None = Field(
        default=None,
        description=(
            "Set when the query joined multiple tables and returned 0 rows -- a "
            "detection-only signal (see agent.state.AgentState.low_confidence_notice), "
            "not necessarily an error."
        ),
    )
    rejection_reason: str | None = None
    rejection_message: str | None = None
    rate_limit_message: str | None = None
    clarification_message: str | None = None
    failure_explanation: str | None = None
    error_history: list[str] = Field(default_factory=list)
    sources_used: list[str] = Field(
        default_factory=list,
        description=(
            "Which source(s) actually contributed (e.g. ['sql'], ['sql', 'policy']). "
            "Only meaningful when the multi-source router is enabled -- empty "
            "otherwise, in which case a client should assume 'sql'."
        ),
    )
    synthesized_answer: str | None = Field(
        default=None,
        description="Combined answer text when 2+ sources contributed; None when only one did.",
    )
    document_result: SourceAnswerOut | None = None
    policy_result: SourceAnswerOut | None = None
    web_result: SourceAnswerOut | None = None
    generation_result: MediaGenerationResultOut | None = None
    media_search_result: MediaSearchResultOut | None = None
    attachment_result: AttachmentResultOut | None = None
    query_plan: list[str] | None = Field(
        default=None,
        description="Ordered plan steps for a complexity-flagged question; None if planning was skipped.",
    )
    schema_tables: list[SchemaTableOut] = Field(default_factory=list)
    followup_classification: Literal["standalone", "followup", "ambiguous"] | None = None
    followup_resolved_against: ConversationExchangeOut | None = None
    permission_denied_notice: str | None = Field(
        default=None,
        description=(
            "Set when the multi-source router picked a source the caller's account "
            "role doesn't have permission for (e.g. media generation requires "
            "analyst/admin) -- that source was dropped and the question was answered "
            "from whatever remained instead, which may not match what was asked. "
            "None when nothing was denied."
        ),
    )


class ExecuteRequest(BaseModel):
    """Validate-and-execute a specific SQL string -- backs the dashboard's
    "Confirm and Run" button. Typically `sql` is a value taken
    from a prior `AskResponse.sql` (verbatim, or hand-edited by the caller)
    and `database` is that same response's `database` field, so execution
    targets the database the SQL was actually generated against."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    sql: str = Field(..., min_length=1)
    database: str | None = Field(
        default=None,
        description=(
            "Settings.databases[i].name to execute against. Omit to use the "
            "first configured database (the same fallback used "
            "when no prior selected_database is available)."
        ),
    )


class GenerateConfirmRequest(BaseModel):
    """The human-approval confirmation step for media generation -- the
    API equivalent of `/execute` above, applied to the "generation" source
    instead of SQL. `question` is typically the exact prompt text a prior
    `AskResponse.generation_result` proposed (its `answer` field, when
    `status == "pending_approval"`), though a caller may edit it before
    confirming, same as `/execute`'s SQL text can be hand-edited first.
    There is deliberately no `media_type` field here -- the kind (image vs.
    video) is always re-inferred server-side from `question`
    (`agent.orchestrator.nodes.infer_media_kind`), never trusted from the
    client, so a caller can't under-report a more expensive video request
    as a cheaper image one."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    question: str = Field(..., min_length=1)


class ChartRecommendationOut(BaseModel):
    """Mirrors `agent.result_charting.ChartRecommendation` -- one suggested
    starting chart type + a short reason, never treated by the frontend as
    proof that type is actually valid for this result (see that module's
    own docstring)."""

    model_config = ConfigDict(frozen=True)

    chart_type: str
    reason: str
    x_column: str | None = None
    y_column: str | None = None


class ExecuteResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["succeeded", "rejected", "failed"]
    database: str
    normalized_sql: str | None = Field(
        default=None,
        description="The validated, row-limited SQL that was actually run (None if rejected/failed).",
    )
    result_columns: list[str] | None = None
    result_rows: list[list[Any]] | None = None
    row_count: int | None = None
    duration_ms: float | None = None
    column_types: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-column inferred type ('numeric' | 'date' | 'text'), from "
            "agent.result_charting.classify_columns -- lets the client's chart "
            "engine (frontend/src/lib/chartEngine.ts) validate and build every "
            "chart type without re-guessing types from raw JSON values."
        ),
    )
    chart_recommendation: ChartRecommendationOut | None = Field(
        default=None,
        description=(
            "A suggested starting chart type + reason (agent.result_charting"
            ".recommend_chart), or None if nothing about this shape suggests "
            "one. Purely a starting point for the client's chart picker -- "
            "never rendered automatically, and never trusted as valid without "
            "the client's own independent validation against the actual rows."
        ),
    )
    truncated: bool = Field(
        default=False,
        description=(
            "True when row_count reached Settings.max_result_rows -- the "
            "result may be missing rows beyond that cap. The UI must show a "
            "visible notice (and a chart, if the user requests one, must "
            "disclose it only reflects the returned/possibly-truncated rows)."
        ),
    )
    error: str | None = None


class GoldenExampleFeedbackRequest(BaseModel):
    """Records a human-approved (question, SQL) pair -- backs the
    dashboard's thumbs-up golden-example feedback widget."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    question: str = Field(..., min_length=1)
    sql: str = Field(..., min_length=1)
    database: str = Field(..., min_length=1)


class GoldenExampleFeedbackResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    saved: bool


class MessageFeedbackRequest(BaseModel):
    """Records a like/dislike (plus an optional free-text comment) on any
    assistant answer -- SQL, document/policy RAG, web search, or media --
    the general-purpose counterpart to `GoldenExampleFeedbackRequest`, which
    only ever covers a confirmed, executed SQL result. Backs
    `frontend/src/components/chat/ResponseFeedbackWidget.tsx`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    question: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    rating: Literal["positive", "negative"]
    sql: str | None = None
    database: str | None = None
    sources_used: list[str] = Field(default_factory=list)
    comment: str | None = None
    conversation_id: str | None = None


class MessageFeedbackResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    saved: bool


class SchemaRefreshResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    database: str
    table_count: int


class SchemaRefreshResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    databases: list[SchemaRefreshResult]


class StageMetricOut(BaseModel):
    """One LangGraph node's rolled-up timing distribution over the current
    in-process window -- see `observability.metrics.StageSummary`, which
    this mirrors field-for-field."""

    model_config = ConfigDict(frozen=True)

    stage: str
    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    total_ms: float


class RequestMetricOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float


class PerformanceMetricsResponse(BaseModel):
    """`GET /metrics/performance` -- a live rollup of the same per-stage
    timing data `agent.nodes._timed_node` has always logged, aggregated by
    `observability.metrics` into a queryable snapshot instead of raw log
    lines. See that module's own docstring for the single-process/
    resets-on-restart limits this deliberately does not claim to solve."""

    model_config = ConfigDict(frozen=True)

    started_at: str
    window_requests: int
    max_window_requests: int
    requests: RequestMetricOut
    stages: list[StageMetricOut]
    status_counts: dict[str, int]


class ComponentHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    detail: str


class DatabaseHealth(BaseModel):
    """Per-configured-database reachability + schema-index status -- one
    entry per `Settings.databases[i]` ('default' for a plain
    single-database setup)."""

    model_config = ConfigDict(frozen=True)

    name: str
    connection: ComponentHealth
    schema_index: ComponentHealth


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "degraded"]
    databases: list[DatabaseHealth]
    ollama: ComponentHealth
    voice_enabled: bool
    media_search_enabled: bool
    # Capability discovery for the React dashboard's login gate
    # (frontend/src/store/localAuthStore.ts) -- same "an infra flag the
    # frontend reads from GET /health, not a build-time guess" shape as
    # voice_enabled/media_search_enabled above. `Settings.local_auth_enabled`
    # alone (not whether AUTH_DATABASE_URL is *reachable*) -- the same
    # "configured, not necessarily healthy" contract those two fields
    # already have.
    local_auth_enabled: bool
    # Startup/operator diagnostic for chat-image vision support -- a real,
    # reported bug (an attached image silently answering "vision not
    # configured" with no way to tell whether that meant "never set up" or
    # "set up but the model isn't actually pulled") is what this closes.
    # `vision_model_available` is `None` whenever `vision_enabled` is
    # False (nothing to check); when True, it's a real, live lookup against
    # Ollama's own `/api/tags` -- reusing the exact same `.list()` call
    # `ollama` health above already makes, not a second round-trip -- so a
    # configured-but-never-pulled model name is caught here, not just
    # discovered the first time a user attaches an image.
    vision_enabled: bool
    vision_provider: Literal["ollama"] | None = None
    vision_model: str | None = None
    vision_model_available: bool | None = None
    ocr_enabled: bool


class ColumnOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    nullable: bool
    is_primary_key: bool


class TableOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    database: str
    table_name: str
    columns: list[ColumnOut]


class TablesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    tables: list[TableOut]


class DocumentOut(BaseModel):
    """Mirrors `rag.store.DocumentRecord` -- one ingested PDF, as listed on
    the Knowledge Sources management page."""

    model_config = ConfigDict(frozen=True)

    id: str
    filename: str
    collection: Literal["documents", "policies"]
    sensitivity_category: str | None = None
    upload_date: str
    status: Literal["processing", "ready", "failed"]
    chunk_count: int
    error_message: str | None = None
    has_pdf_bytes: bool
    # 2026 Phase 3 security review -- see rag.store.DocumentRecord's own
    # docstring for what each means (uploaded_by: audit trail only, never
    # used to scope access; restricted_roles: an actual access-control
    # gate, enforced by rag/graph.py and api/documents.py's download route).
    uploaded_by: str | None = None
    restricted_roles: list[str] | None = None


class DocumentListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    documents: list[DocumentOut]


class DocumentUploadResponse(BaseModel):
    """Mirrors `rag.ingestion.IngestionResult` -- the per-file outcome shown
    in the Knowledge Sources upload UI."""

    model_config = ConfigDict(frozen=True)

    document_id: str | None
    filename: str
    status: Literal["ready", "failed"]
    chunk_count: int
    warnings: list[str] = Field(default_factory=list)
    error_message: str | None = None


class TranscribeResponse(BaseModel):
    """Output of `POST /voice/transcribe`. `text` is the raw, untrusted
    Whisper transcript. `corrected_text` is an AI-cleaned suggestion
    (misheard words/filler words fixed, gated by
    `Settings.enable_voice_correction`) -- `None` when correction is
    disabled or produced no actual change, so the frontend only needs to
    branch on "is there a suggestion" rather than compare strings itself.
    Neither field is pre-validated or submitted anywhere -- the caller
    shows both and only submits back through `POST /ask` once the user
    explicitly confirms which text to send."""

    model_config = ConfigDict(frozen=True)

    text: str
    corrected_text: str | None
    stt_duration_ms: float


class SynthesizeRequest(BaseModel):
    """Input to `POST /voice/synthesize`. `text` length is capped by
    `Settings.max_question_length` at the route level, the same cap
    `AskRequest.question` already relies on -- not duplicated here as a
    schema-level `Field(max_length=...)` for the same reason
    `AskRequest.question` doesn't either."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    text: str = Field(..., min_length=1)
