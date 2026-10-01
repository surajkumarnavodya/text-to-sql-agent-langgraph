"""Wires the agent nodes into a compiled LangGraph state machine.

    sanitize_input -> classify_followup -> retrieve_schema -> retrieve_golden_examples -> retrieve_business_context -> classify_analytical_intent -> build_analytical_plan -> plan_query -> generate_sql -+-> review_sql -+-> review_metric_conformance -+-> validate_sql -+-> estimate_cost -+-> execute_sql -+-> compute_analytics -> generate_forecast -> generate_recommendations -> generate_insight -> END
         |                    |                    ^                                                                  |               ^                              |                 ^                |                  ^                 |                |
         |                    |                    +----------------------------------(retry, up to max_retries)------+---------------+------------------------------+-----------------+---(retry, high    |               |
         |                    |                                                                                       |                                                                                    cost only)      +--(retry, only  |
         |                    +-> END (needs_clarification, ambiguous)                                                |                                                                                                        on a missing_  |
         |                                                                                                             +-> END (rejected --                                                                                         reference       |
         +-> END (rejected -- too_long/empty/injection_detected/off_topic)                                                OffTopicQuestionError backstop)                                                                           error)

The retry budget itself ("max_retries" in the diagram above) is not always
`Settings.max_retries` -- `run_agent()` computes an effective, possibly
larger budget once per question via `agent.complexity.compute_max_retries`
and stores it in `state["max_retries"]`, which every retry-vs-give-up check
(`review_sql_node`/`review_metric_conformance_node`/`validate_sql_node`/
`estimate_query_cost_node`/`execute_sql_node`) reads instead of the raw
setting. See `agent/complexity.py`'s module docstring.

`review_metric_conformance` (between `review_sql` and `validate_sql`,
Prompt 10, `10_GOVERNED_METRICS_CONTRACT.md`) checks the generated SQL
against every governing (PUBLISHED) metric definition
`retrieve_business_context` surfaced for this question
(`state["governing_metrics"]`) -- a pure pass-through, zero added
latency, whenever there's no governing metric to check against (the
overwhelming common case) or `Settings.enable_metric_conformance_review`
is off. A FAIL shares the identical retry-budget machinery
`review_sql_node` already uses. See `agent.nodes
.review_metric_conformance_node`.

`retrieve_golden_examples` (between `retrieve_schema` and `plan_query`) looks
up human-approved (question, SQL) pairs a user previously saved via the UI's
thumbs-up feedback (see `embeddings.golden_examples`), and, when any clear
the configured similarity threshold, injects them into `generate_sql`'s
prompt as few-shot examples on top of the static `_QUERY_PATTERNS_BLOCK`.
Fails open exactly like `plan_query`/`review_sql` below -- disabled via
`Settings.enable_golden_examples`, an empty/unreachable store, or no example
clearing the threshold all resolve to "no examples," never a reason a
question can't be answered. See `agent.nodes.retrieve_golden_examples_node`.

`retrieve_business_context` (between `retrieve_golden_examples` and
`classify_analytical_intent`) looks up semantically-relevant business context -- table/
column/relationship descriptions, glossary terms, metric definitions,
curated SQL examples, and documentation snippets -- from the vector
retrieval layer in `retrieval/` (see `docs/vector-retrieval-design.md`),
injecting whatever it finds into `generate_sql`'s prompt as a clearly
labeled, "verify against the live schema" block
(`agent.llm_client._build_business_context_block`). This is additive
context only: the live schema (`schema_context_text`, from
`retrieve_schema` above) remains the sole authority on what tables/columns
actually exist, and SQL validation/execution are completely unaffected by
whatever this node returns. Fails open exactly like `retrieve_golden_examples`
right above it -- a disabled feature flag, an empty/missing collection, or
any retrieval error all resolve to `retrieved_context = []` plus a logged,
state-visible warning (`retrieval_warnings`), never a reason a question
can't be answered. See `agent.nodes.retrieve_business_context_node`.

`classify_analytical_intent` (between `retrieve_business_context` and
`build_analytical_plan`, Prompt 11, `11_ANALYTICAL_INTENT_CONTRACT.md`)
classifies what *kind* of analytical question this is (LOOKUP/AGGREGATION/
TREND/COMPARISON/RANKING/.../RECOMMENDATION -- see `agent/intent.py`), on
*every* question when `Settings.enable_intent_classification` is on
(unlike `plan_query` below, gated by `agent.complexity`'s signals) --
fails open (an unreachable Ollama server or an unparseable response)
to `state["analytical_intent"] = None`, never a reason a question can't
be answered. Its consumers are `build_analytical_plan_node`'s and
`plan_query_node`'s shared gate/prompt (see below) -- it is purely
advisory, additive to `agent.complexity`'s own trip-wires, never a
replacement for them, and never touches authorization or the retry
budget. See `agent.nodes.classify_analytical_intent_node`.

`build_analytical_plan` (between `classify_analytical_intent` and
`plan_query`, Prompt 12, `12_ANALYTICAL_PLANNING_CONTRACT.md`) is the
**first tier** of a three-tier planning fallback: for a question the
exact same gate below judges non-trivial, it makes an LLM call producing
a structured `agent.analytical_plan.AnalyticalPlan` (metric(s),
dimensions, filters, time range, grain, comparison, ranking, sort,
limit, required database capabilities), then **deterministically
re-verifies every claim it makes** (`agent.plan_validator.validate_plan`
-- table/column existence, FK-declared relationship paths, sensitive-
column policy + caller authorization, time feasibility, target-database
capability support) before ever showing it to `generate_sql`. A plan
that fails validation is discarded in full, not partially trusted --
`state["analytical_plan"]` stays `None` and the question falls through
to tier two (`plan_query`'s own free-text planning, completely
unmodified) exactly as it would have before this node existed. On
success, also renders the validated plan into `state["query_plan"]`
(`agent.analytical_plan.render_plan_as_steps`) so `review_sql_node`'s
existing plan-conformance check keeps working unmodified against it,
rather than adding a second review node. Gated by `Settings
.enable_analytical_planning` on top of the shared trigger below. See
`agent.nodes.build_analytical_plan_node` for the full reasoning.

`plan_query` (between `build_analytical_plan` and `generate_sql`) and `review_sql`
(between `generate_sql` and `review_metric_conformance`) are the agentic
query-decomposition + plan-conformance self-correction pair: `plan_query_node`
makes an up-front LLM call that breaks a *non-trivial* question -- one that
matched an `agent.complexity` signal, OR one `classify_analytical_intent`
classified as a kind that itself implies planning (`agent.intent
.intent_implies_planning`) -- into an ordered plan, which is then
injected into `generate_sql`'s own prompt; `review_sql_node` makes a second
LLM call checking whether the generated SQL actually implements that plan,
looping back to `generate_sql` with a targeted critique (sharing the same
`max_retries` budget as every other retryable failure) if not. Both nodes
are a pure pass-through -- no LLM call, zero added latency -- for the
overwhelming common case of a question that matched no complexity signal
and no intent implying planning, or when `Settings.enable_query_planning`
is off. `plan_query_node` additionally skips its own LLM call (but stays a
pass-through either way) whenever `build_analytical_plan` already produced
a validated structured plan for this question -- see
`agent.nodes.plan_query_node`/`review_sql_node` for the full reasoning,
including the fail-open behavior on an unreachable Ollama server or an
unparseable plan/verdict.

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
limit from the question-submission one `api/main.py` enforces per client
before `run_agent()` is even called; see `agent/rate_limit.py`'s docstring
for why the retry loop specifically needs its own limiter.

`compute_analytics` (between `execute_sql` and `generate_insight`, Prompt
13, `13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md`) is the Deterministic
Analytical Result Engine: on every successful execution (gated only by
`Settings.enable_analytics_engine`, no `enable_insight`-style skip, since
this is a pure, zero-I/O computation with no narrative cost to gate on)
it classifies the result's shape and computes the full statistic set
`analytics.engine.compute_analytics_result` covers (row/null/distinct
counts, min/max/mean/median/variance/stddev/percentiles, a full
period-by-period growth series, a complete ranking, and both z-score and
IQR outliers), storing it on `state["analytical_result"]`. Fails open on
any unexpected error, the same posture every other accuracy-aid node
here takes. Deliberately does **not** feed `generate_insight`'s own LLM
prompt in this pass -- see `agent.nodes.compute_analytics_node`'s own
docstring for the disclosed, benchmark-stability reason.

`generate_forecast` (between `compute_analytics` and `generate_insight`,
Prompt 16, `16_FORECASTING_CONTRACT.md`) forecasts future periods for a
time-series-shaped result, but -- unlike `compute_analytics` right before
it -- only when `classify_analytical_intent` already classified the
question as `AnalyticalIntentType.FORECAST` (reusing that judgment, never
re-classifying). Reuses the exact `GrowthStat.points` series
`compute_analytics` already computed (zero new queries); a data-
insufficient or untrusted-temporal-semantics series produces a normal,
typed rejection rather than a guess. Gated by `Settings.enable_forecasting`
on top of the intent check; a straight edge, never a conditional one, so
this node can never short-circuit the graph. See `agent.nodes
.generate_forecast_node`.

`generate_recommendations` (between `generate_forecast` and
`generate_insight`, Prompt 17, `17_RECOMMENDATION_ENGINE_CONTRACT.md`) is
the Evidence-First Recommendation Engine: on every successful execution
(gated only by `Settings.enable_recommendation_engine`) it runs
`recommendation.engine.generate_recommendations` over whichever typed
evidence this request already has in hand (`state["analytical_result"]`,
`state["cost_estimate"]`, a live `observability.metrics` snapshot, any
restricted-column reference in the final SQL) -- zero new queries, zero
new LLM calls. A candidate without evidence, or below the configured
confidence floor, or that the running caller isn't authorized to see, is
dropped before construction; see `recommendation.engine`'s own module
docstring for the full Data -> Finding -> Evidence -> Rule -> Candidate
-> Validation -> Confidence -> Recommendation pipeline. A straight edge,
never a conditional one, so this node can never short-circuit the graph.
Fails open on any unexpected error, the same posture every other
accuracy-aid node here takes. See `agent.nodes.generate_recommendations_node`.

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
import time
from functools import lru_cache

from langgraph.graph import END, StateGraph

from agent.complexity import compute_max_retries
from agent.nodes import (
    build_analytical_plan_node,
    classify_analytical_intent_node,
    classify_followup_node,
    compute_analytics_node,
    estimate_query_cost_node,
    execute_sql_node,
    generate_forecast_node,
    generate_insight_node,
    generate_recommendations_node,
    generate_sql_node,
    plan_query_node,
    retrieve_business_context_node,
    retrieve_golden_examples_node,
    retrieve_schema_node,
    review_metric_conformance_node,
    review_sql_node,
    route_after_classification,
    route_after_cost_estimate,
    route_after_execution,
    route_after_generation,
    route_after_metric_conformance,
    route_after_review,
    route_after_sanitization,
    route_after_validation,
    sanitize_input_node,
    validate_sql_node,
)
from agent.state import AgentState, ConversationExchange
from config.settings import get_settings
from observability.metrics import get_default_metrics

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def build_graph():
    """Constructs and compiles the LangGraph state graph, once per process.

    `StateGraph.compile()`'s result is stateless -- all per-question state
    lives in the `initial_state` dict `run_agent()` passes to `.invoke()`,
    never on the compiled graph object itself -- so building it once and
    reusing it (the same `functools`-based singleton pattern already used
    for `config.settings.get_settings()` and `db.connection._cached_engine`)
    is safe and avoids re-wiring all eighteen nodes on every single question.
    Graph *shape* never depends on `Settings` (`retrieve_golden_examples`/
    `classify_analytical_intent`/`build_analytical_plan`/`plan_query`/
    `review_sql`/`review_metric_conformance`/`compute_analytics`/
    `generate_forecast`/`generate_recommendations` are pass-throughs, not
    conditionally-omitted nodes, when their respective flag (or, for
    `generate_forecast`, its intent gate) doesn't fire -- see this
    module's docstring), so
    there's no per-settings cache key to worry about; like every other
    process-lifetime singleton here, a config change that would matter
    takes a process restart.

    Returns:
        A compiled LangGraph graph exposing `.invoke(state)`.
    """
    graph = StateGraph(AgentState)

    graph.add_node("sanitize_input", sanitize_input_node)
    graph.add_node("classify_followup", classify_followup_node)
    graph.add_node("retrieve_schema", retrieve_schema_node)
    graph.add_node("retrieve_golden_examples", retrieve_golden_examples_node)
    graph.add_node("retrieve_business_context", retrieve_business_context_node)
    graph.add_node("classify_analytical_intent", classify_analytical_intent_node)
    graph.add_node("build_analytical_plan", build_analytical_plan_node)
    graph.add_node("plan_query", plan_query_node)
    graph.add_node("generate_sql", generate_sql_node)
    graph.add_node("review_sql", review_sql_node)
    graph.add_node("review_metric_conformance", review_metric_conformance_node)
    graph.add_node("validate_sql", validate_sql_node)
    graph.add_node("estimate_cost", estimate_query_cost_node)
    graph.add_node("execute_sql", execute_sql_node)
    graph.add_node("compute_analytics", compute_analytics_node)
    graph.add_node("generate_forecast", generate_forecast_node)
    graph.add_node("generate_recommendations", generate_recommendations_node)
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
    graph.add_edge("retrieve_schema", "retrieve_golden_examples")
    graph.add_edge("retrieve_golden_examples", "retrieve_business_context")
    graph.add_edge("retrieve_business_context", "classify_analytical_intent")
    graph.add_edge("classify_analytical_intent", "build_analytical_plan")
    graph.add_edge("build_analytical_plan", "plan_query")
    graph.add_edge("plan_query", "generate_sql")
    graph.add_conditional_edges(
        "generate_sql",
        route_after_generation,
        {
            "review_sql": "review_sql",
            "rejected": END,
            "failed": END,
            "rate_limited": END,
        },
    )
    graph.add_conditional_edges(
        "review_sql",
        route_after_review,
        {
            "review_metric_conformance": "review_metric_conformance",
            "generate_sql": "generate_sql",
            "failed": END,
        },
    )
    graph.add_conditional_edges(
        "review_metric_conformance",
        route_after_metric_conformance,
        {
            "validate_sql": "validate_sql",
            "generate_sql": "generate_sql",
            "failed": END,
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
            "succeeded": "compute_analytics",
            "generate_sql": "generate_sql",
            "retrieve_schema": "retrieve_schema",
            "failed": END,
        },
    )
    graph.add_edge("compute_analytics", "generate_forecast")
    graph.add_edge("generate_forecast", "generate_recommendations")
    graph.add_edge("generate_recommendations", "generate_insight")
    graph.add_edge("generate_insight", END)

    return graph.compile()


def run_agent(
    question: str,
    conversation_history: list[ConversationExchange] | None = None,
    enable_insight: bool = True,
    caller_roles: tuple[str, ...] = (),
    model: str | None = None,
    tenant_id: str | None = None,
    forecast_horizon: int | None = None,
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
        caller_roles: The authenticated caller's roles (`security.oidc
            .AuthIdentity.roles`, set by `api/auth.py`) -- `()` (the
            default) for a caller with no elevated permissions, which is
            also what a script calling this directly (`eval/runner.py`,
            `scripts/integration_test.py`) gets without passing anything.
            Read by `validate_sql_node`'s restricted-column gate (2026
            Phase 2 security review) via `agent.authz.has_permission` --
            see `docs/AUTHORIZATION.md`.
        model: The Ollama model to use for every LLM call this run makes
            (generation, planning, review, insight) -- `AskRequest.model`,
            already validated against `Settings.ollama_allowed_models` by
            `api/main.py`'s `/ask` handler (via `agent.model_registry
            .validate_model_selection`) *before* this function is ever
            called, so an invalid/disallowed model never reaches the graph.
            `None` (the default -- every caller before this feature existed,
            and any current caller that omits `AskRequest.model`) uses
            `Settings.ollama_model` unchanged; `eval/runner.py` and
            `scripts/integration_test.py` both get this default too, since
            neither passes anything for this parameter. Resolved once here,
            stored in `state["selected_model"]`, and never re-resolved
            per-retry -- see that field's own docstring in `agent/state.py`
            for why a question's model choice, like its `selected_database`,
            must stay fixed for the life of one run.
        tenant_id: The authenticated caller's tenant, resolved once by the
            caller (`api/main.py`'s `/ask` handler, via
            `security.tenancy.resolve_tenant_id_for_identity`) exactly like
            `caller_roles` above. `None` (the default -- every caller before
            this field existed) means "no tenant to resolve," the same
            "no elevated permissions"-shaped default `caller_roles=()`
            already establishes. Stored in `state["tenant_id"]`; nothing
            reads it yet -- see that field's own docstring in
            `agent/state.py` for why it exists ahead of any real
            enforcement.
        forecast_horizon: How many future periods `generate_forecast_node`
            should forecast, already validated against `Settings
            .forecast_max_horizon` by `api/main.py`'s `/ask` handler
            (`AskRequest.forecast_horizon`) before this function is ever
            called -- the identical pattern `model` above already uses.
            `None` (the default -- every caller before this field existed)
            defers to `Settings.forecast_default_horizon`, resolved by
            `analytics.forecasting.generate_forecast` itself. Stored in
            `state["forecast_horizon"]`; only read when
            `generate_forecast_node` actually attempts a forecast (see
            that node's own docstring for the full gate).
    """
    settings = get_settings()
    effective_model = model or settings.ollama_model
    # Computed once, up front -- not re-evaluated per retry -- so a
    # question's budget is fixed for the life of this run regardless of how
    # the question text might read differently in combination with a later
    # error message. See agent.complexity's module docstring for why this
    # exists: certain question shapes (top-N-per-group, period-over-period
    # growth, several metrics at once) are more likely to need more than
    # one or two self-correction cycles.
    effective_max_retries, complexity_signals = compute_max_retries(
        question, settings.max_retries, settings.complex_query_max_retry_bonus
    )
    logger.info(
        "Starting agent run for question=%r conversation_history_len=%d enable_insight=%s "
        "max_retries=%d (base=%d) complexity_signals=%s model=%s",
        question,
        len(conversation_history or []),
        enable_insight,
        effective_max_retries,
        settings.max_retries,
        complexity_signals,
        effective_model,
    )
    compiled_graph = build_graph()
    initial_state: AgentState = {
        "question": question,
        "caller_roles": caller_roles,
        "tenant_id": tenant_id,
        "rejection_reason": None,
        "rejection_message": None,
        "rate_limit_message": None,
        "conversation_history": conversation_history or [],
        "followup_classification": None,
        "followup_resolved_against": None,
        "clarification_message": None,
        "selected_database": None,
        "selected_model": effective_model,
        "retrieved_context": [],
        "retrieval_query": None,
        "retrieval_sources": [],
        "retrieval_warnings": [],
        "retrieval_metadata": {},
        "governing_metrics": [],
        "analytical_intent": None,
        "analytical_plan": None,
        "analytical_plan_violations": None,
        "query_plan": None,
        "plan_review_passed": None,
        "plan_review_feedback": None,
        "enable_insight": enable_insight,
        "insight": None,
        "insight_summary": None,
        "schema_anomaly_tables": [],
        "cost_estimate": None,
        "cost_notice": None,
        "low_confidence_notice": None,
        "analytical_result": None,
        "forecast_horizon": forecast_horizon,
        "forecast_result": None,
        "recommendations": [],
        "retry_count": 0,
        "max_retries": effective_max_retries,
        "complexity_signals": complexity_signals,
        "error_history": [],
        "attempt_history": [],
        "last_error_category": None,
        "failure_explanation": None,
        "status": "pending",
    }
    # LangGraph's own default recursion_limit (25 total node executions) is
    # not automatically related to this graph's *own* retry budget
    # (effective_max_retries above) -- a real, reproduced bug: a worst-case
    # retry sequence (an execute_sql "missing_reference" retry loops all the
    # way back to retrieve_schema -- a 12-node cycle: retrieve_schema,
    # retrieve_golden_examples, retrieve_business_context,
    # classify_analytical_intent, build_analytical_plan, plan_query,
    # generate_sql, review_sql, review_metric_conformance, validate_sql,
    # estimate_cost, execute_sql)
    # can exceed 25 total steps well before
    # effective_max_retries is exhausted -- e.g. the ~15-step initial pass
    # plus just two such retries already exceeds that. When that happened,
    # LangGraph raised an uncaught GraphRecursionError instead of the
    # graph reaching its own intended terminal "failed" state, which
    # surfaced to callers as an unhandled 500 rather than a normal failure
    # response. Sized generously for the worst-case (every retry being the
    # 10-node missing_reference cycle) plus headroom, scaled to this
    # question's actual effective_max_retries (which can itself be raised
    # above the base Settings.max_retries by agent.complexity's adaptive
    # bonus) so a future config change can't reintroduce this.
    recursion_limit = 20 + effective_max_retries * 10
    run_start = time.perf_counter()
    final_state = compiled_graph.invoke(initial_state, config={"recursion_limit": recursion_limit})
    total_duration_ms = (time.perf_counter() - run_start) * 1000
    logger.info(
        "Agent run finished: status=%s retries=%d followup_classification=%s has_insight=%s "
        "rejection_reason=%s",
        final_state.get("status"),
        final_state.get("retry_count", 0),
        final_state.get("followup_classification"),
        final_state.get("insight") is not None,
        final_state.get("rejection_reason"),
    )
    # Purely additive observability -- feeds the already-computed
    # stage_timings into the live rollup GET /metrics/performance reads.
    # Never allowed to fail the request it's instrumenting: a metrics-
    # recording bug must degrade to "this one run's data is missing from
    # the rollup," never to "the user's question failed," matching this
    # codebase's standing fail-open posture for every other accuracy/
    # observability aid (see observability/metrics.py's own docstring).
    try:
        get_default_metrics().record_agent_run(
            final_state.get("stage_timings", []), total_duration_ms, final_state.get("status")
        )
    except Exception:
        logger.warning("Failed to record performance metrics for this run", exc_info=True)
    return final_state
