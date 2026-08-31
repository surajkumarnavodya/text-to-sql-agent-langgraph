"""Wires the agent nodes into a compiled LangGraph state machine.

    retrieve_schema -> generate_sql -> validate_sql -+-> execute_sql -+-> END (succeeded)
         ^                    ^                       |                |
         |                    |                       +---(retry)------+
         |                    +---------------(retry, up to max_retries)
         +----------(retry, only on a missing_reference execution error --
                      see agent/nodes.py::route_after_execution)

Two failure shapes never loop back at all and go straight to END (failed):
a validator SAFETY_VIOLATION_TYPES rejection (non-SELECT, stacked query,
SELECT INTO -- a security gate, not worth retrying) and a query TIMEOUT
(retrying an expensive query rarely helps). See agent/nodes.py for the
per-category routing logic.

`run_agent()` is the single public entry point the UI (and tests) should
call -- it hides graph construction so callers don't need to know LangGraph
to use the agent.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from agent.nodes import (
    execute_sql_node,
    generate_sql_node,
    retrieve_schema_node,
    route_after_execution,
    route_after_validation,
    validate_sql_node,
)
from agent.state import AgentState

logger = logging.getLogger(__name__)


def build_graph():
    """Constructs and compiles the LangGraph state graph.

    Returns:
        A compiled LangGraph graph exposing `.invoke(state)`.
    """
    graph = StateGraph(AgentState)

    graph.add_node("retrieve_schema", retrieve_schema_node)
    graph.add_node("generate_sql", generate_sql_node)
    graph.add_node("validate_sql", validate_sql_node)
    graph.add_node("execute_sql", execute_sql_node)

    graph.set_entry_point("retrieve_schema")
    graph.add_edge("retrieve_schema", "generate_sql")
    graph.add_edge("generate_sql", "validate_sql")

    graph.add_conditional_edges(
        "validate_sql",
        route_after_validation,
        {
            "execute_sql": "execute_sql",
            "generate_sql": "generate_sql",
            "failed": END,
        },
    )
    graph.add_conditional_edges(
        "execute_sql",
        route_after_execution,
        {
            "succeeded": END,
            "generate_sql": "generate_sql",
            "retrieve_schema": "retrieve_schema",
            "failed": END,
        },
    )

    return graph.compile()


def run_agent(question: str) -> AgentState:
    """Runs the full agent graph for a single natural-language question.

    Args:
        question: The user's natural-language question.

    Returns:
        The final `AgentState` after the graph reaches `END` -- check
        `state["status"]` ("succeeded" or "failed") and
        `state["error_history"]` for the outcome.
    """
    logger.info("Starting agent run for question=%r", question)
    compiled_graph = build_graph()
    initial_state: AgentState = {
        "question": question,
        "retry_count": 0,
        "error_history": [],
        "attempt_history": [],
        "last_error_category": None,
        "failure_explanation": None,
        "status": "pending",
    }
    final_state = compiled_graph.invoke(initial_state)
    logger.info(
        "Agent run finished: status=%s retries=%d",
        final_state.get("status"),
        final_state.get("retry_count", 0),
    )
    return final_state
