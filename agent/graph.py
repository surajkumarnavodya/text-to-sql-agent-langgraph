"""Wires the agent nodes into a compiled LangGraph state machine.

    sanitize_input -> classify_followup -> retrieve_schema -> generate_sql -+-> validate_sql -+-> estimate_cost -+-> execute_sql -+-> generate_insight -> END
         |                    |                    ^                       |                  ^                 |                |
         |                    |                    +-----(retry, up to max_retries)------------+---(retry, high  |               |
         |                    |                                            |                      cost only)    +--(retry, only  |
         |                    +-> END (needs_clarification, ambiguous)     |                                       on a missing_  |
         |                                                                 +-> END (rejected --                    reference       |
         +-> END (rejected -- too_long/empty/injection_detected/off_topic)    OffTopicQuestionError backstop)       error)

Three failure shapes never loop back at all and go straight to END: a
validator SAFETY_VIOLATION_TYPES rejection and a `generate_sql` llm_error
(both -> "failed"; retrying either wastes budget on something a retry can't
fix), and a query TIMEOUT (-> "failed", same reason). See agent/nodes.py for
the per-category routing logic.

`estimate_cost` runs a non-executing EXPLAIN/SHOWPLAN estimate on the
validated SQL (see `db.query_cost` and `agent.nodes.
estimate_query_cost_node`) -- an earlier, additional layer in front of
`execute_sql`'s existing timeout, not a replacement for it. Low/moderate
severity proceeds to `execute_sql` normally (moderate just adds a UI
notice); "high" severity never executes at all -- it's treated exactly
like any other retryable correctness mistake, sharing the same
`max_retries` budget as a parse or syntax error, so the model gets a
chance to add a filter on its own before the agent gives up.

`sanitize_input` is the true entry point -- see `agent.nodes.
sanitize_input_node` and `agent.input_guard` for the length/Unicode-
normalization/injection-pattern/off-topic gate that runs before anything
else touches the question, including `classify_followup`'s own text
analysis. `classify_followup` is a cheap, regex-only heuristic (see
`agent.followup.classify_followup`) deciding whether the (already
sanitized) question is standalone, a follow-up to the most recent prior
exchange, or ambiguous. Only "ambiguous" changes the graph's shape from
there -- it ends immediately at `needs_clarification` rather than spending
a schema-retrieval + generation cycle on a guess. Standalone and follow-up
both continue into `retrieve_schema` exactly as before.

A second, independent off-topic check happens inside `generate_sql` itself:
the system prompt instructs the model to refuse (via a fixed sentinel) if a
question isn't answerable as SQL, which `generate_sql_node` turns into the
same "rejected" terminal state as `sanitize_input`'s pre-filter -- a
defense-in-depth backstop for anything the cheaper regex pre-filter missed,
not the normal path (see `agent.exceptions.OffTopicQuestionError`).

`generate_sql` also checks a process-wide LLM-call rate limiter (`agent.
rate_limit.get_llm_call_limiter`) before every attempt, including retries --
a denial ends the run immediately at `status="rate_limited"` (see
`route_after_generation`), never retried. This is a separate, stricter
limit from the question-submission one `ui/app.py` enforces per session
before `run_agent()` is even called; see `agent/rate_limit.py`'s docstring
for why the retry loop specifically needs its own limiter.

`generate_insight` is the only node reachable from execute_sql's *success*
path -- a failed, needs-clarification, or rejected run never generates one.
It is a narrative layer only: see `agent.nodes.generate_insight_node` for
how it stays strictly grounded in (and never influences) the already-final
sql/result_rows/row_count.

`run_agent()` is the single public entry point the UI (and tests) should
call -- it hides graph construction so callers don't need to know LangGraph
to use the agent.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from agent.nodes import (
    classify_followup_node,
    estimate_query_cost_node,
    execute_sql_node,
    generate_insight_node,
    generate_sql_node,
    retrieve_schema_node,
    route_after_classification,
    route_after_cost_estimate,
    route_after_execution,
    route_after_generation,
    route_after_sanitization,
    route_after_validation,
    sanitize_input_node,
    validate_sql_node,
)
from agent.state import AgentState, ConversationExchange

logger = logging.getLogger(__name__)


def build_graph():
    """Constructs and compiles the LangGraph state graph.

    Returns:
        A compiled LangGraph graph exposing `.invoke(state)`.
    """
    graph = StateGraph(AgentState)

    graph.add_node("sanitize_input", sanitize_input_node)
    graph.add_node("classify_followup", classify_followup_node)
    graph.add_node("retrieve_schema", retrieve_schema_node)
    graph.add_node("generate_sql", generate_sql_node)
    graph.add_node("validate_sql", validate_sql_node)
    graph.add_node("estimate_cost", estimate_query_cost_node)
    graph.add_node("execute_sql", execute_sql_node)
    graph.add_node("generate_insight", generate_insight_node)

    graph.set_entry_point("sanitize_input")
    graph.add_conditional_edges(
        "sanitize_input",
        route_after_sanitization,
        {
            "classify_followup": "classify_followup",
            "rejected": END,
        },
    )
    graph.add_conditional_edges(
        "classify_followup",
        route_after_classification,
        {
            "retrieve_schema": "retrieve_schema",
            "needs_clarification": END,
        },
    )
    graph.add_edge("retrieve_schema", "generate_sql")
    graph.add_conditional_edges(
        "generate_sql",
        route_after_generation,
        {
            "validate_sql": "validate_sql",
            "rejected": END,
            "failed": END,
            "rate_limited": END,
        },
    )

    graph.add_conditional_edges(
        "validate_sql",
        route_after_validation,
        {
            "estimate_cost": "estimate_cost",
            "generate_sql": "generate_sql",
            "failed": END,
        },
    )
    graph.add_conditional_edges(
        "estimate_cost",
        route_after_cost_estimate,
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
            "succeeded": "generate_insight",
            "generate_sql": "generate_sql",
            "retrieve_schema": "retrieve_schema",
            "failed": END,
        },
    )
    graph.add_edge("generate_insight", END)

    return graph.compile()


def run_agent(
    question: str,
    conversation_history: list[ConversationExchange] | None = None,
    enable_insight: bool = True,
) -> AgentState:
    """Runs the full agent graph for a single natural-language question.

    Args:
        question: The user's natural-language question.
        conversation_history: Recent prior exchanges in this session (oldest
            first), already capped by the caller -- see
            `ui/session_history.py::build_conversation_history`, which is the
            single source of truth this is built from (the UI's History
            panel and follow-up resolution both read the same underlying
            session state, not two parallel copies). None/empty means "no
            prior context," which `classify_followup_node` treats the same
            as an empty list.
        enable_insight: Whether `generate_insight_node` should attempt a
            plain-English insight after a successful execution. True by
            default (normally low-risk, high-value); the UI exposes this as
            a toggle. False skips the extra LLM call entirely rather than
            just hiding the result.

    Returns:
        The final `AgentState` after the graph reaches `END` -- check
        `state["status"]` ("succeeded", "failed", "needs_clarification",
        "rejected", or "rate_limited") and `state["error_history"]` for the
        outcome.
    """
    logger.info(
        "Starting agent run for question=%r conversation_history_len=%d enable_insight=%s",
        question,
        len(conversation_history or []),
        enable_insight,
    )
    compiled_graph = build_graph()
    initial_state: AgentState = {
        "question": question,
        "rejection_reason": None,
        "rejection_message": None,
        "rate_limit_message": None,
        "conversation_history": conversation_history or [],
        "followup_classification": None,
        "followup_resolved_against": None,
        "clarification_message": None,
        "enable_insight": enable_insight,
        "insight": None,
        "insight_summary": None,
        "schema_anomaly_tables": [],
        "cost_estimate": None,
        "cost_notice": None,
        "retry_count": 0,
        "error_history": [],
        "attempt_history": [],
        "last_error_category": None,
        "failure_explanation": None,
        "status": "pending",
    }
    final_state = compiled_graph.invoke(initial_state)
    logger.info(
        "Agent run finished: status=%s retries=%d followup_classification=%s has_insight=%s "
        "rejection_reason=%s",
        final_state.get("status"),
        final_state.get("retry_count", 0),
        final_state.get("followup_classification"),
        final_state.get("insight") is not None,
        final_state.get("rejection_reason"),
    )
    return final_state
