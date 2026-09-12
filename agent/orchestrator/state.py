"""Shared state for the top-level multi-source orchestrator graph.

`OrchestratorState` extends `agent.state.AgentState` rather than replacing
it: when the SQL subgraph runs, its full result (`status`, `sql`,
`result_rows`, `error_history`, ...) is merged straight into this state under
the exact same keys `AgentState` already uses. That's what lets every
existing `ui/app.py` read site (`state["status"]`, `state["sql"]`, the retry
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
            `agent.orchestrator.nodes.get_available_sources`) -- mirrors
            `embeddings.retriever.select_database`'s single-database
            short-circuit, so a plain SQL-only setup never pays for a
            classification call it doesn't need.
    """

    sources: list[str]
    reasoning: str
    short_circuited: bool


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
    # Fetch the actual bytes via `GET /media/{media_id}`
    # (`api/media.py`) or, in-process from `ui/app.py`,
    # `media_gen.cache.get_media_cache().get(media_id)`. None whenever
    # `status != "succeeded"`.
    media_id: str | None
    media_type: str | None
    model: str | None


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

    # Set by router_node -- see RouteDecision above.
    route_decision: RouteDecision | None

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

    # Set by synthesis_node -- None when only one source fired (that source's
    # own answer stands unedited; see synthesis_node's docstring).
    synthesized_answer: str | None
