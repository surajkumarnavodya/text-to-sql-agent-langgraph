"""POST /analyst/investigate -- the AI Data Analyst agent (Prompt 33).

Behind `Settings.enable_data_analyst_agent` (default off). When the flag is
off the route answers 404, so the endpoint does not exist from a client's
point of view. The existing `/ask` path is unaffected either way.

Authorization: the analyst executes SQL, so the route requires both
`Permission.ASK` and `Permission.EXECUTE_SQL`. Every sub-question still passes
the full governed pipeline with the caller's own roles, so restricted columns
and the SQL validator apply exactly as they do on `/ask`.

Admission control: the analyst shares `/ask`'s in-flight limiter, so an
analysis cannot starve the chat endpoint, and a request past the cap gets the
same 429 `/ask` returns.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent.analyst.budget import AnalystBudget, usage_summary
from agent.analyst.graph import _now, run_analysis
from agent.authz import Permission
from agent.input_guard import sanitize_conversation_history
from agent.model_registry import InvalidModelSelectionError, validate_model_selection
from agent.multiagent.supervisor import run_supervised_analysis
from agent.rate_limit import get_ask_concurrency_limiter
from agent.state import ConversationExchange
from api.analyst_schemas import (
    AnalystEvidenceOut,
    AnalystRecommendationOut,
    AnalystRequest,
    AnalystResponse,
    AnalystSubquestionOut,
    AnalystTraceOut,
    AnalystUsageOut,
    SupervisedClaimOut,
    SupervisedConflictOut,
    SupervisedResponse,
    SupervisedSubquestionOut,
)
from api.authz import require_permission
from api.rate_limit import enforce_api_action_rate_limit
from config.settings import Settings, get_settings
from security.oidc import AuthIdentity, real_caller_subject
from security.tenancy import resolve_tenant_id_for_identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analyst", tags=["analyst"])

ANALYST_DISABLED_DETAIL = "The AI Data Analyst agent is not enabled on this server."
ANALYST_CONCURRENCY_MESSAGE = (
    "The server is busy with other questions. Please try again in a moment."
)


def budget_from_settings(settings: Settings) -> AnalystBudget:
    """Builds this request's hard limits from `Settings`. Clients cannot
    supply or raise these."""
    return AnalystBudget(
        max_steps=settings.analyst_max_steps,
        max_subqueries=settings.analyst_max_subqueries,
        max_llm_calls=settings.analyst_max_llm_calls,
        max_followups=settings.analyst_max_followups,
        timeout_seconds=settings.analyst_timeout_seconds,
    )


@router.post("/investigate", response_model=AnalystResponse)
def investigate(
    payload: AnalystRequest,
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
    _execute: AuthIdentity = Depends(require_permission(Permission.EXECUTE_SQL)),
) -> AnalystResponse:
    """Runs one bounded, multi-step analysis of a business question and
    returns its evidence-linked report.

    Every intermediate artifact is in the response's `trace`. The analysis
    always ends with a report, even when a budget stops it early, and that
    report states what did not run.
    """
    settings = get_settings()
    if not settings.enable_data_analyst_agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ANALYST_DISABLED_DETAIL)

    enforce_api_action_rate_limit(request, "analyst_investigate", settings, identity)

    try:
        selected_model = validate_model_selection(payload.model, settings)
    except InvalidModelSelectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    history = sanitize_conversation_history(
        [
            ConversationExchange(
                question=turn.question, sql=turn.sql, tables=turn.tables, status=turn.status
            )
            for turn in payload.conversation_history
        ],
        settings.max_question_length,
    )

    limiter = get_ask_concurrency_limiter(settings.max_concurrent_ask_requests)
    if not limiter.try_acquire():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ANALYST_CONCURRENCY_MESSAGE,
            headers={"Retry-After": "2"},
        )
    try:
        state = run_analysis(
            payload.question,
            budget=budget_from_settings(settings),
            caller_roles=identity.roles,
            caller_subject=real_caller_subject(identity),
            tenant_id=resolve_tenant_id_for_identity(identity),
            model=selected_model,
            conversation_history=history,
        )
    finally:
        limiter.release()

    usage = usage_summary(state["usage"], _now())
    return AnalystResponse(
        status=state.get("status") or "failed",
        stop_reason=state.get("stop_reason"),
        planning_mode=state.get("planning_mode"),
        subquestions=[AnalystSubquestionOut(**sq) for sq in state.get("subquestions", [])],
        evidence=[AnalystEvidenceOut(**item) for item in state.get("evidence", [])],
        recommendations=[
            AnalystRecommendationOut(**rec) for rec in state.get("recommendations", [])
        ],
        open_items=list(state.get("open_items") or []),
        trace=[AnalystTraceOut(**entry) for entry in state.get("trace", [])],
        usage=AnalystUsageOut(**usage),
        report_markdown=state.get("report_markdown") or "",
    )


SUPERVISOR_DISABLED_DETAIL = "The multi-agent supervisor is not enabled on this server."


@router.post("/supervise", response_model=SupervisedResponse)
def supervise(
    payload: AnalystRequest,
    request: Request,
    identity: AuthIdentity = Depends(require_permission(Permission.ASK)),
    _execute: AuthIdentity = Depends(require_permission(Permission.EXECUTE_SQL)),
) -> SupervisedResponse:
    """Runs one turn through the multi-agent supervisor (Prompt 34).

    The same authorization, rate limit, model validation, history sanitization
    and admission control as `/analyst/investigate`. Turn budgets come from
    `Settings`. A response is always returned, even when a budget or a failing
    specialist stops part of the turn, and the report says what did not run.
    """
    settings = get_settings()
    if not settings.enable_multi_agent_supervisor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=SUPERVISOR_DISABLED_DETAIL
        )

    enforce_api_action_rate_limit(request, "analyst_supervise", settings, identity)

    try:
        selected_model = validate_model_selection(payload.model, settings)
    except InvalidModelSelectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    history = sanitize_conversation_history(
        [
            ConversationExchange(
                question=turn.question, sql=turn.sql, tables=turn.tables, status=turn.status
            )
            for turn in payload.conversation_history
        ],
        settings.max_question_length,
    )

    limiter = get_ask_concurrency_limiter(settings.max_concurrent_ask_requests)
    if not limiter.try_acquire():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ANALYST_CONCURRENCY_MESSAGE,
            headers={"Retry-After": "2"},
        )
    try:
        result = run_supervised_analysis(
            payload.question,
            caller_roles=identity.roles,
            caller_subject=real_caller_subject(identity),
            tenant_id=resolve_tenant_id_for_identity(identity),
            model=selected_model,
            conversation_history=history,
        )
    finally:
        limiter.release()

    return SupervisedResponse(
        status=result.status,
        stop_reason=result.stop_reason,
        subquestions=[SupervisedSubquestionOut(**sq) for sq in result.subquestions],
        claims=[
            SupervisedClaimOut(
                agent=claim.agent.value,
                task_id=claim.task_id,
                text=claim.text,
                truth_level=claim.truth_level.value,
                grounded_in=list(claim.grounded_in),
            )
            for claim in result.claims
        ],
        conflicts=[
            SupervisedConflictOut(
                task_id=c.task_id, key=c.key, agents=list(c.agents), outcome=c.outcome
            )
            for c in result.conflicts
        ],
        violation_count=len(result.violations),
        open_items=result.open_items,
        trace=result.trace,
        usage=result.usage,
        report_markdown=result.report_markdown,
    )
