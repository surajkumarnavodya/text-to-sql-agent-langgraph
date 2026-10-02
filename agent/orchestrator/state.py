"""Shared state for the top-level multi-source orchestrator graph.

`OrchestratorState` extends `agent.state.AgentState` rather than replacing
it: when the SQL subgraph runs, its full result (`status`, `sql`,
`result_rows`, `error_history`, ...) is merged straight into this state under
the exact same keys `AgentState` already uses. That's what lets every
existing read site (`state["status"]`, `state["sql"]`, the retry
timeline, ...) keep working unchanged regardless of whether a question went
through `agent.graph.run_agent` directly or through the orchestrator -- see
`agent/orchestrator/graph.py::run_orchestrated`.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from agent.state import AgentState
from rag.graph import Citation


class RouteDecision(TypedDict):
    """The router's decision for one question.

    Attributes:
        sources: Which source(s) the question was routed to, e.g. ["sql"]
            or ["sql", "policy"].
        reasoning: A short, human-readable explanation -- the routing log
            line and the UI's "Sources used" panel both read this directly
            rather than re-deriving it.
        short_circuited: True when routing skipped classification entirely
            because fewer than two sources are configured (see
            `agent.orchestrator.nodes.get_available_sources`), or because
            the deterministic attachment-only pre-check matched (see
            `agent.orchestrator.nodes._looks_like_attachment_only_question`)
            -- either way, no LLM classification call was made for this
            question.
        requires_database: Whether "sql" ended up in `sources` -- a plain,
            debuggable summary of "did this question actually touch the
            database," independent of parsing `sources` yourself. Always
            `False` for a question the deterministic attachment-only
            pre-check routed, since that path never includes "sql".
    """

    sources: list[str]
    reasoning: str
    short_circuited: bool
    requires_database: bool


class SourceAnswer(TypedDict):
    """One non-SQL source's contribution -- document_rag, policy_rag, or web_search.

    Deliberately the same shape for all three, so `synthesis_node` and the
    UI's "Sources used" panel don't need source-specific branches to read it.
    """

    answer: str
    citations: list[Citation]
    status: str


class MediaGenerationResult(SourceAnswer):
    """The "generation" source's contribution -- extends `SourceAnswer`
    (`citations` is always `[]`, unused, kept only so `synthesis_node`'s
    existing per-source loop can read `answer`/`status` identically to
    document_result/policy_result/web_result with no special-casing) with
    the fields specific to a generated media artifact. See
    `agent.orchestrator.nodes.generation_node` and `media_gen/`.
    """

    # An opaque `media_gen.cache.MediaCache` id, never the provider's raw
    # CDN URL -- `generation_node` downloads the bytes once and stores them
    # under this id (see `media_gen/cache.py`/`media_gen/download.py`).
    # Fetch the actual bytes via `GET /media/{media_id}` (`api/media.py`).
    # None whenever `status != "succeeded"`.
    media_id: str | None
    media_type: str | None
    model: str | None


class MediaSearchHit(TypedDict):
    """One retrieved image or video segment, trimmed to exactly what the
    UI/answer-composition prompt need -- mirrors `media.search.MediaHit`
    but never carries its internal `similarity` score (that stays purely
    an internal ranking detail, never surfaced to the user, same as no
    other source here ever shows a raw retrieval score)."""

    media_id: str
    media_type: str
    caption: str
    timestamp_start: float | None
    timestamp_end: float | None


class AttachmentResult(SourceAnswer):
    """The "attachments" source's contribution -- files the user attached
    directly to this question (see `attachments/graph.py`), extending
    `SourceAnswer` with which attachment(s) actually contributed and
    whether an image had to fall back to OCR-only text because no vision
    model is configured (`Settings.media_vision_model`)."""

    used_attachment_ids: list[str]
    vision_unavailable: bool


class MediaSearchResult(SourceAnswer):
    """The "media_search" source's contribution -- extends `SourceAnswer`
    (a real citable text answer, unlike `MediaGenerationResult`'s -- see
    `agent.orchestrator.nodes.media_search_node`) with the retrieved hits
    the UI renders as its own thumbnail-grid component
    (`MediaSearchResultCard.tsx`), the same "always render outside the
    synthesis-text ternary" rule `generation_result` already established.
    """

    hits: list[MediaSearchHit]


class OrchestratorState(AgentState, total=False):
    """Full state threaded through the orchestrator graph."""

    # Set once by run_orchestrated from its own `session_id` parameter --
    # a real (if untrusted) per-conversation correlation token
    # (AskRequest.session_id), used by router_node to scope the
    # session-level expensive-source cost ceiling (see
    # agent.rate_limit.get_session_expensive_source_limiter). None for a
    # caller that never supplied one (e.g. eval/runner.py, scripts) -- the
    # ceiling simply doesn't apply when there's no session to scope it to.
    session_id: str | None

    # Input, set once by run_orchestrated from AskRequest.attachment_ids --
    # files the caller attached directly to this question (see
    # attachments/graph.py). Empty list means no attachments. A non-empty
    # value both makes "attachments" available to router_node (forced into
    # the final route regardless of what the classifier picks -- see that
    # node's docstring) and, per agent/orchestrator/graph.py's
    # run_orchestrated, makes this graph run at all even when
    # Settings.enable_multi_source_router is off -- attaching a file is a
    # per-request opt-in, not a standing multi-source-routing decision.
    pending_attachment_ids: list[str]

    # Set by router_node -- see RouteDecision above.
    route_decision: RouteDecision | None

    # Set by router_node whenever the LLM's own source selection included a
    # source the caller's role(s) don't have permission for (see
    # agent.orchestrator.nodes.SOURCE_PERMISSIONS) -- a short, human-
    # readable explanation that request was answered from a different,
    # possibly-mismatched source because of a role restriction, not a
    # genuine failure of the source it actually used. None when nothing was
    # denied. Surfaced as-is in AskResponse.permission_denied_notice so the
    # frontend can show an honest reason instead of (or alongside) whatever
    # generic failure the fallback source produced.
    permission_denied_notice: str | None

    # Which source(s) actually contributed to the final answer, accumulated
    # via the same operator.add reducer pattern AgentState's error_history/
    # attempt_history already use, so a multi-source run collects
    # contributions from every subgraph that fired rather than the last one
    # to run overwriting the others.
    sources_used: Annotated[list[str], operator.add]

    # Set by document_rag_node/policy_rag_node -- see rag/graph.py::RagState
    # for what actually produces these (SourceAnswer is a trimmed view of it).
    document_result: SourceAnswer | None
    policy_result: SourceAnswer | None
    # Set by web_search_node -- see search/web_search.py.
    web_result: SourceAnswer | None
    # Set by generation_node -- see media_gen/ and Settings.enable_media_generation.
    generation_result: MediaGenerationResult | None
    # Set by media_search_node -- see media/ and Settings.enable_media_search.
    media_search_result: MediaSearchResult | None
    # Set by attachment_node -- see attachments/graph.py and Settings.enable_chat_attachments.
    attachment_result: AttachmentResult | None

    # Set by synthesis_node -- None when only one source fired (that source's
    # own answer stands unedited; see synthesis_node's docstring).
    synthesized_answer: str | None
