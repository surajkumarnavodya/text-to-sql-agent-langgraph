"""Wires the orchestrator nodes into a compiled top-level LangGraph state machine.

    router -+-> sql_subgraph  -+
            |   (agent.graph.run_agent -- unmodified)
            +-> document_rag   +-> synthesis -> END
            |   (rag.graph.run_rag, collection="documents")
            +-> policy_rag     |
            |   (rag.graph.run_rag, collection="policies")
            +-> web_search    -+
                (search.web_search.web_search)

`route_after_router` returns a *list* of destination node names -- LangGraph
runs every one of them as a parallel branch before the graph proceeds to
`synthesis`, so a genuinely multi-source question ("compare policy X with
the database") fans out to two (or more) subgraphs in the same graph step,
not one after another (confirmed against this project's pinned LangGraph
version before this was built, not assumed).

`run_orchestrated` is the single public entry point `ui/app.py` and
`api/main.py` call in place of `agent.graph.run_agent` directly. It is
deliberately a two-path function, not a graph with one trivial branch:

  - `Settings.enable_multi_source_router` is False (the default -- see
    `.env.example`): `run_orchestrated` calls `agent.graph.run_agent`
    directly and returns its result completely unwrapped. This is not "the
    orchestrator graph with one destination" -- it is the exact same
    function call `ui/app.py` made before this package existed, so a fresh
    clone with today's `.env` behaves identically to today's app, per
    CLAUDE.md's Part 7 constraint. `eval/runner.py` and the standalone
    scripts also keep calling `run_agent` directly and are entirely
    unaffected by anything in this package.
  - True: the orchestrator graph below actually runs. With only `sql`
    configured (the common case even with the flag on), `router_node`
    short-circuits to `sql_subgraph_node`, which itself just calls
    `run_agent` -- so a SQL-only setup's *results* are unchanged even with
    the router on; what changes is that routing now happens through an
    inspectable graph step (logged, and shown in the UI's "Sources used"
    panel) instead of a bare function call.

See `agent/orchestrator/nodes.py` for what each node does and
`agent/orchestrator/state.py` for why `OrchestratorState` extends
`AgentState` rather than replacing it.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from agent.graph import run_agent
from agent.orchestrator.nodes import (
    document_rag_node,
    policy_rag_node,
    route_after_router,
    router_node,
    sql_subgraph_node,
    synthesis_node,
    web_search_node,
)
from agent.orchestrator.state import OrchestratorState
from agent.state import AgentState, ConversationExchange
from config.settings import get_settings

logger = logging.getLogger(__name__)


def build_orchestrator_graph():
    """Constructs and compiles the orchestrator's top-level LangGraph graph.

    Returns:
        A compiled LangGraph graph exposing `.invoke(state)`.
    """
    graph = StateGraph(OrchestratorState)

    graph.add_node("router", router_node)
    graph.add_node("sql_subgraph", sql_subgraph_node)
    graph.add_node("document_rag", document_rag_node)
    graph.add_node("policy_rag", policy_rag_node)
    graph.add_node("web_search", web_search_node)
    graph.add_node("synthesis", synthesis_node)

    graph.set_entry_point("router")
    graph.add_conditional_edges(
        "router",
        route_after_router,
        {
            "sql_subgraph": "sql_subgraph",
            "document_rag": "document_rag",
            "policy_rag": "policy_rag",
            "web_search": "web_search",
        },
    )
    for destination in ("sql_subgraph", "document_rag", "policy_rag", "web_search"):
        graph.add_edge(destination, "synthesis")
    graph.add_edge("synthesis", END)

    return graph.compile()


def run_orchestrated(
    question: str,
    conversation_history: list[ConversationExchange] | None = None,
    enable_insight: bool = True,
) -> AgentState | OrchestratorState:
    """Routes a question to one or more sources and returns the combined result.

    Args:
        question: The user's natural-language question -- passed through
            unmodified to whichever source(s) end up handling it (each
            source does its own input handling; `agent.graph.run_agent`
            already sanitizes on the SQL path exactly as it always has).
        conversation_history: Same shape and meaning as
            `agent.graph.run_agent`'s parameter of the same name.
        enable_insight: Same shape and meaning as `agent.graph.run_agent`'s
            parameter of the same name -- only consulted by the SQL
            destination today.

    Returns:
        When `Settings.enable_multi_source_router` is off, exactly what
        `agent.graph.run_agent` returns (an `AgentState`). When on, an
        `OrchestratorState` -- a superset of `AgentState`'s keys, plus
        `route_decision`, `sources_used`, and (when 2+ sources fired)
        `synthesized_answer` -- so `state["status"]`, `state["sql"]`, etc.
        are readable identically either way.
    """
    settings = get_settings()
    if not settings.enable_multi_source_router:
        return run_agent(question, conversation_history, enable_insight)

    logger.info("Starting orchestrated run for question=%r", question)
    compiled_graph = build_orchestrator_graph()
    # OrchestratorState inherits every AgentState field (see state.py), so
    # its accumulator channels (error_history/attempt_history/stage_timings/
    # sources_used, each an `operator.add` reducer) are initialized here too
    # -- mirroring agent.graph.run_agent's own initial_state exactly, even
    # on the common single-branch path. Same defensive convention, same
    # reason: never rely on a reducer channel's implicit empty state.
    initial_state: OrchestratorState = {
        "question": question,
        "rejection_reason": None,
        "rejection_message": None,
        "rate_limit_message": None,
        "conversation_history": conversation_history or [],
        "followup_classification": None,
        "followup_resolved_against": None,
        "clarification_message": None,
        "selected_database": None,
        "enable_insight": enable_insight,
        "insight": None,
        "insight_summary": None,
        "schema_anomaly_tables": [],
        "cost_estimate": None,
        "cost_notice": None,
        "low_confidence_notice": None,
        "retry_count": 0,
        "error_history": [],
        "attempt_history": [],
        "last_error_category": None,
        "failure_explanation": None,
        "status": "pending",
        "stage_timings": [],
        "route_decision": None,
        "sources_used": [],
        "document_result": None,
        "policy_result": None,
        "web_result": None,
        "synthesized_answer": None,
    }
    final_state = compiled_graph.invoke(initial_state)
    logger.info(
        "Orchestrated run finished: status=%s sources_used=%s",
        final_state.get("status"),
        final_state.get("sources_used"),
    )
    return final_state
