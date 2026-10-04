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
    (`Settings.max_question_length`, default 5000, overridable via `.env`)
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
    model: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Ollama model to use for this question's SQL generation/planning/review/insight "
            "calls, from a prior GET /models response's `models[].id`. Omit to use the "
            "server-configured default (Settings.ollama_model) -- every existing caller that "
            "predates this field keeps working unchanged. Validated server-side against "
            "Settings.ollama_allowed_models (agent.model_registry.validate_model_selection) "
            "before this question is ever run -- an unrecognized/disallowed value is rejected "
            "with HTTP 400, never silently substituted or passed through to Ollama as-is."
        ),
    )
    forecast_horizon: int | None = Field(
        default=None,
        gt=0,
        description=(
            "How many future periods to forecast, only meaningful when this question is "
            "classified as a FORECAST-intent question (see agent.intent.AnalyticalIntentType) "
            "-- ignored otherwise. Omit to use the server-configured default "
            "(Settings.forecast_default_horizon); every existing caller that predates this "
            "field keeps working unchanged. Validated server-side against "
            "Settings.forecast_max_horizon before this question is ever run -- a value above "
            "that cap is rejected with HTTP 400, never silently clamped."
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
    image_blur: bool
    image_text_removal: bool
    image_text_removal_method: str | None = None
    image_ai_editing: bool
    image_ai_editing_provider: str | None = None
    native_pdf_input: bool = False
    max_image_bytes: int
    max_document_bytes: int
    max_attachments_per_message: int
    max_total_attachment_bytes: int
    max_resize_dimension_px: int
    max_text_removal_regions: int
    max_ai_edit_prompt_length: int
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


class BlurRegionRequest(BaseModel):
    """`POST /attachments/{id}/blur-region` -- deterministic, local Pillow
    Gaussian blur over a painted mask region. `mask_data_url` follows this
    app's canonical mask convention (`attachments.mask`'s own docstring):
    the painted/opaque region is blurred, everything else is left
    byte-identical. Never a model call -- see that route's own docstring
    for why this stays local/free even when AI-guided editing is enabled."""

    model_config = ConfigDict(extra="forbid")

    mask_data_url: str = Field(..., min_length=1)
    radius: int = Field(default=18, gt=0, le=100)


AiImageEditOperation = Literal[
    "remove_object", "replace_background", "replace_sky", "region_edit", "enhance"
]


class AiImageEditRequest(BaseModel):
    """`POST /attachments/{id}/ai-edit` -- a real, generative edit via a
    configured external provider (see `media_gen.image_edit_provider`'s own
    docstring for which one and why). `{attachment_id}` in the URL is
    always a fresh, transient attachment the client uploaded moments
    earlier via `POST /attachments/upload`, holding the *current* edited
    canvas state (crop/rotate/local-annotations already baked in) -- never
    the original file, which could be stale relative to what the editor's
    preview actually shows (see `ImageEditor.tsx`'s own "always snapshot
    the current canvas, never the original" contract)."""

    model_config = ConfigDict(extra="forbid")

    operation: AiImageEditOperation
    prompt: str = Field(..., min_length=1)
    mask_data_url: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


class AiImageEditResponse(BaseModel):
    """Mirrors `attachments.ai_edit.ImageEditOutcome`. Deliberately never
    raises an HTTP 5xx for an ordinary provider failure (a timeout, a
    content-policy rejection, a rate limit) -- `status="failed"` plus a
    safe `error_code`/`error_message` is the normal shape for "the edit
    didn't work," matching `MediaGenerationResultOut`'s own
    always-200-with-a-status-field convention for the sibling media-
    generation feature."""

    model_config = ConfigDict(frozen=True)

    operation: str
    status: Literal["completed", "failed"]
    source_attachment_id: str
    mask_provided: bool
    attachment_id: str | None = None
    image_data_url: str | None = None
    media_type: str | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    provider: str | None = None
    model: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


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


class ForecastPointOut(BaseModel):
    """Mirrors `analytics.models.ForecastPoint`."""

    model_config = ConfigDict(frozen=True)

    period: str
    forecast: float
    lower_bound: float | None = None
    upper_bound: float | None = None
    horizon_step: int


class ForecastEvaluationOut(BaseModel):
    """Mirrors `analytics.models.ForecastEvaluation`."""

    model_config = ConfigDict(frozen=True)

    holdout_size: int
    mae: float
    rmse: float
    mape: float | None = None
    formula: str


class ForecastModelMetadataOut(BaseModel):
    """Mirrors `analytics.models.ForecastModelMetadata`."""

    model_config = ConfigDict(frozen=True)

    model: str
    version: str
    parameters: dict[str, Any]
    training_window_start: str
    training_window_end: str
    training_point_count: int
    period_kind: str
    supports_interval: bool
    confidence_level: float


class ForecastResultOut(BaseModel):
    """Mirrors `analytics.models.ForecastResult` -- Prompt 16
    (`16_FORECASTING_CONTRACT.md`). Unlike `analytical_result` above (a raw
    `dict[str, Any]`), this gets a fully typed response shape since a
    forecast's structure is smaller and more directly client-relevant
    (points to plot, a model name/horizon to label, limitations to
    disclose) -- the same reasoning `VisualizationSpecOut` already applies
    over `analytical_result`'s own raw-dict shortcut.

    Always `truth_level == "ai_inference"` (`DataTruthLevel.AI_INFERENCE`)
    -- a client must never render `points`/`summary` as if they were
    confirmed fact; `limitations` is always non-empty for a `status="ok"`
    result (Prompt 16's own "forecasts are explicitly represented as
    estimates with limitations" acceptance criterion).
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "rejected"]
    rejection_reasons: tuple[str, ...] = ()
    horizon: int
    model: ForecastModelMetadataOut | None = None
    points: tuple[ForecastPointOut, ...] = ()
    evaluation: ForecastEvaluationOut | None = None
    candidate_evaluations: dict[str, ForecastEvaluationOut] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    truth_level: str
    summary: str = ""
    engine_version: str


class AnalyticalIntentOut(BaseModel):
    """Mirrors `agent.intent.AnalyticalIntentClassification` -- Prompt 11's
    per-question classification, always `ai_inference`. Surfaced so the
    analytics dashboard can choose which panels to emphasize (a TREND
    question leads with the trend panel, a RANKING question with the
    ranking panel) and show an honest ambiguity notice -- never a
    confirmed fact."""

    model_config = ConfigDict(frozen=True)

    intent: str
    confidence: float
    metric_candidates: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    time_requirement: str | None = None
    comparison: str | None = None
    filters: tuple[str, ...] = ()
    expected_result_shape: str | None = None
    ambiguity_flags: tuple[str, ...] = ()
    truth_level: str


class PlanMetricOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    table: str | None = None
    column: str | None = None
    aggregation: str = "none"
    governed_metric_key: str | None = None


class PlanDimensionOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    table: str
    column: str


class PlanFilterOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    table: str
    column: str
    operator: str
    value: str


class PlanTimeRangeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    table: str
    column: str
    description: str


class PlanComparisonOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    description: str


class PlanRankingOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_by: str
    direction: str = "desc"
    top_n: int | None = None
    per_group: tuple[str, ...] = ()


class PlanSortOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    direction: str = "desc"


class AnalyticalPlanOut(BaseModel):
    """Mirrors `agent.analytical_plan.AnalyticalPlan` -- Prompt 12's
    deterministically re-verified plan. Read-only context for the analytics
    dashboard's "query scope" display (metrics, breakdown dimensions,
    filters, time window, ranking) -- never a way to change the query."""

    model_config = ConfigDict(frozen=True)

    metrics: tuple[PlanMetricOut, ...] = ()
    dimensions: tuple[PlanDimensionOut, ...] = ()
    filters: tuple[PlanFilterOut, ...] = ()
    time_range: PlanTimeRangeOut | None = None
    grain: str | None = None
    comparison: PlanComparisonOut | None = None
    ranking: PlanRankingOut | None = None
    sort: tuple[PlanSortOut, ...] = ()
    limit: int | None = None
    required_operations: tuple[str, ...] = ()
    truth_level: str


class GoverningMetricOut(BaseModel):
    """One published, `CONFIRMED_BUSINESS_TRUTH` metric definition this
    question was answered against -- mirrors the dicts
    `retrieval.retriever.extract_governing_metrics` returns. `approved_expression`
    is the reviewed definition itself, shown as the semantic definition the
    answer is grounded in."""

    model_config = ConfigDict(frozen=True)

    business_name: str
    approved_expression: str | None = None
    aggregation: str | None = None
    text: str


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
    message_id: str | None = Field(
        default=None,
        description=(
            "The persisted identity.models.AiOutput.id for this turn's answer -- set only "
            "alongside conversation_id (a locally-authenticated caller whose turn was "
            "actually persisted). Pass this back as ExecuteRequest.message_id when the "
            "caller later confirms and runs this turn's SQL, so the confirmed result can be "
            "attached to this exact saved turn -- see api/chat_persistence.py."
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
    model: str | None = Field(
        default=None,
        description=(
            "The Ollama model actually used for this question's LLM calls -- either "
            "AskRequest.model (once validated) or Settings.ollama_model if that field "
            "was omitted. Always set on a run that reached generation."
        ),
    )
    sql: str | None = None
    result_columns: list[str] | None = None
    result_rows: list[list[Any]] | None = None
    row_count: int | None = None
    retry_count: int = 0
    attempt_history: list[AttemptRecordOut] = Field(default_factory=list)
    insight: str | None = None
    analytical_result: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The full deterministic statistical breakdown of the result "
            "(analytics.models.AnalyticsResult.model_dump()'s shape -- row/null/distinct "
            "counts, min/max/mean/median/variance/stddev/percentiles per column, a "
            "period-by-period growth series, a ranking, and z-score/IQR outliers, all "
            "DataTruthLevel.DATABASE_FACT) -- see agent.state.AgentState.analytical_result. "
            "Not yet rendered by the chat UI or the insight narrative text; surfaced here "
            "for a client that wants to build its own view on top of it. None when "
            "Settings.enable_analytics_engine is off or the run didn't reach a successful "
            "execution."
        ),
    )
    forecast_result: ForecastResultOut | None = Field(
        default=None,
        description=(
            "analytics.forecasting.generate_forecast's output -- set only when this question "
            "was classified as a FORECAST-intent question (agent.intent.AnalyticalIntentType) "
            "and Settings.enable_forecasting is on; None otherwise, including for every "
            "question type that predates this field. A 'rejected' status means the result "
            "wasn't a usable time series or didn't have enough history -- see "
            "rejection_reasons. Always DataTruthLevel.AI_INFERENCE -- never render points/"
            "summary as confirmed fact (see ForecastResultOut's own docstring)."
        ),
    )
    recommendations: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "recommendation.engine.generate_recommendations's output -- one dict per "
            "recommendation.models.Recommendation.model_dump(), each carrying its own "
            "finding/evidence/affected_entity/action/measurable_impact/confidence/"
            "rule_or_model/limitations/category (one of nine: performance, anomaly, "
            "revenue, customer, product, operations, data_quality, security, "
            "database_performance). Always AI_INFERENCE (agent.provenance.DataTruthLevel), "
            "grounded in the DATABASE_FACT/CONFIRMED_BUSINESS_TRUTH evidence it carries -- "
            "never render as confirmed fact. Empty (never a sentinel) when "
            "Settings.enable_recommendation_engine is off, the run didn't reach a "
            "successful execution, or no candidate cleared evidence/confidence/"
            "authorization validation."
        ),
    )
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
    answer_duration_ms: float | None = Field(
        default=None,
        description=(
            "Server-side wall time of this question's pipeline run, in milliseconds. "
            "Persisted with the turn, so a reopened conversation shows the same figure "
            "the live answer did. Null when the request never reached the pipeline."
        ),
    )
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
    # Prompt 30 (30_ANALYTICS_INSIGHTS_DASHBOARD_CONTRACT.md) -- all four are
    # additive, all default to "nothing to report," and every one is a
    # pass-through of state a prior prompt already computed and never exposed.
    analytical_intent: AnalyticalIntentOut | None = Field(
        default=None,
        description=(
            "Prompt 11's classification of what kind of analytical question this was "
            "(agent.intent.AnalyticalIntentClassification). Always ai_inference -- a "
            "classification, never a confirmed fact. None when intent classification "
            "is disabled or unavailable."
        ),
    )
    analytical_plan: AnalyticalPlanOut | None = Field(
        default=None,
        description=(
            "Prompt 12's deterministically validated plan (metrics, dimensions, filters, "
            "time range, ranking). Read-only context for display -- never a way to change "
            "the executed query. None when planning was skipped or the plan failed "
            "deterministic validation (in which case no plan was ever trusted)."
        ),
    )
    governing_metrics: list[GoverningMetricOut] = Field(
        default_factory=list,
        description=(
            "Published, CONFIRMED_BUSINESS_TRUTH metric definitions this question was "
            "answered against (Prompt 10). Empty when no governed metric matched -- the "
            "common case."
        ),
    )
    restricted_field_notice: str | None = Field(
        default=None,
        description=(
            "Set when this question's generated SQL repeatedly referenced a column the "
            "caller's role may not view (VIEW_RESTRICTED_COLUMNS), so the answer could "
            "not be produced. A presentation-only translation of an already-computed "
            "failure category -- the restricted-column check itself and its retry "
            "behavior are unchanged. None otherwise."
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
    conversation_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "AskResponse.conversation_id from the /ask call this SQL came from, if any -- "
            "used only as a defense-in-depth cross-check alongside message_id below (see "
            "api/chat_persistence.py::persist_execute_result). Never required, never used "
            "to select which database/rows to execute against."
        ),
    )
    message_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "AskResponse.message_id from the /ask call this SQL came from, if any -- when "
            "supplied by a locally-authenticated caller, a successful execution's result "
            "(bounded rows, chart recommendation, the actually-executed SQL) is attached to "
            "that already-persisted turn, so reopening the conversation later shows it "
            "immediately without re-running anything. Omitting it changes nothing about "
            "execution itself -- this only affects what gets remembered for later."
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


class ChartFieldOut(BaseModel):
    """Mirrors `analytics.visualization.ChartField`."""

    model_config = ConfigDict(frozen=True)

    column: str
    role: str
    aggregation: str | None = None
    format: str = "plain"


class ChartSortOut(BaseModel):
    """Mirrors `analytics.visualization.ChartSort`."""

    model_config = ConfigDict(frozen=True)

    column: str
    direction: str


class AccessibilityMetadataOut(BaseModel):
    """Mirrors `analytics.visualization.AccessibilityMetadata`."""

    model_config = ConfigDict(frozen=True)

    alt_text: str
    summary: str


class VisualizationSpecOut(BaseModel):
    """Mirrors `analytics.visualization.ChartSpec` -- a richer, additive
    sibling to `ChartRecommendationOut` above (chart type, fields/roles,
    inferred aggregation/format, title, sort, a deterministic top-N limit,
    and non-empty accessibility metadata). Deliberately NOT consumed by
    `frontend/src/lib/chartEngine.ts`'s own seed-selection in this pass --
    `chart_recommendation`/`column_types` keep powering today's chart
    picker completely unchanged (see that module's own "must not regress"
    invariant in CLAUDE.md); this is parallel, additive API surface for a
    future wiring pass."""

    model_config = ConfigDict(frozen=True)

    chart_type: str
    title: str
    fields: tuple[ChartFieldOut, ...]
    sort: ChartSortOut | None = None
    limit: int | None = None
    is_downsampled: bool = False
    notices: tuple[str, ...] = ()
    accessibility: AccessibilityMetadataOut
    reason: str
    engine_version: str


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
    cache_status: Literal["hit", "miss"] | None = Field(
        default=None,
        description=(
            "Prompt 30 -- the same signal as the X-Cache response header (Prompt 22's "
            "result cache), mirrored into the JSON body so a client that reads only "
            "JSON can tell a served-from-cache result from a live one. 'hit' means no "
            "database round trip happened for this response. None when the result "
            "cache is disabled or this SQL is not cacheable."
        ),
    )
    visualization_spec: VisualizationSpecOut | None = Field(
        default=None,
        description=(
            "A fuller deterministic chart specification (analytics.visualization"
            ".build_chart_spec) -- chart type, fields/roles, inferred aggregation/"
            "format, title, sort, a top-N limit, and accessibility metadata. "
            "Additive and parallel to chart_recommendation/column_types above, "
            "which remain the client's actual chart-picker seed; not yet "
            "consumed by the frontend."
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
    # Prompt 06 (06_DATABASE_DISCOVERY_CONTRACT.md) -- all additive, all
    # optional with a value that means "nothing to report," so an older
    # client that doesn't know these fields yet sees no behavior change.
    view_count: int = 0
    added_tables: list[str] = []
    removed_tables: list[str] = []
    changed_tables: list[str] = []
    last_discovered_at: str | None = None


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
    # Prompt 22 (scale/performance hardening) -- cumulative, process-
    # lifetime counters, additive to the rolling-window fields above. See
    # `observability.metrics.MetricsSnapshot`'s own docstring.
    result_cache_hits: int = 0
    result_cache_misses: int = 0
    database_concurrency_rejections: int = 0
    # Prompt 20 (multi-tenant) -- which tenant this rollup covers. Always
    # the requesting caller's own tenant over HTTP; the field exists so a
    # response is self-describing about its scope rather than leaving a
    # reader to assume it is process-wide (which it no longer is).
    tenant_id: str | None = None


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
    # Whether "Anyone with the link" sharing is allowed on this server. Lets the
    # share dialog hide an option the server would otherwise refuse with a 400.
    share_public_links_enabled: bool = False
    # Capability discovery for the React dashboard's login gate
    # (frontend/src/store/localAuthStore.ts) -- same "an infra flag the
    # frontend reads from GET /health, not a build-time guess" shape as
    # voice_enabled/media_search_enabled above. `Settings.local_auth_enabled`
    # alone (not whether AUTH_DATABASE_URL is *reachable*) -- the same
    # "configured, not necessarily healthy" contract those two fields
    # already have.
    local_auth_enabled: bool
    # Google sign-in capability discovery (2026-09-28) -- same "sanitized
    # capability endpoint" contract as the rest of this response:
    # `google_client_id` is a PUBLIC OAuth client ID, never a secret (see
    # `security/google_oidc.py`'s own module docstring for why this flow
    # never holds a client secret at all), and is only ever populated
    # alongside `google_signin_enabled=True` -- the frontend's "Continue
    # with Google" button reads both together and never assumes a client
    # ID implies availability or the reverse.
    google_signin_enabled: bool = False
    google_client_id: str | None = None
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


class ModelOut(BaseModel):
    """One selectable Ollama model, as `GET /models` reports it -- mirrors
    `agent.model_registry.ModelOption`. `installed`/`available` are a live
    lookup against the *connected* Ollama instance (never the online Ollama
    Library, which this application never queries at runtime -- see
    `agent/model_registry.py`'s module docstring); a model can be
    `enabled=True` (configured/allowed) but `installed=False` (never
    `ollama pull`ed on this machine) -- the frontend must disable selecting
    it in that state rather than assuming `enabled` alone means usable."""

    model_config = ConfigDict(frozen=True)

    id: str
    display_name: str
    is_default: bool
    enabled: bool = True
    installed: bool
    available: bool
    recommended: bool = False
    parameter_size: str = ""
    context_length: int | None = None
    resource_level: str = "unknown"
    capabilities: tuple[str, ...] = ()
    description: str = ""


class ModelsResponse(BaseModel):
    """`GET /models` -- the configured/allowed Text-to-SQL model registry,
    each enriched with live local-installation status. Never includes
    secrets/connection details/environment variables -- see that route's
    own docstring in `api/main.py`."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["ollama"] = "ollama"
    default_model: str
    selection_enabled: bool
    models: list[ModelOut]


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
