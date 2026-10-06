"""The AI Data Analyst agent: a bounded LangGraph loop over the governed SQL
pipeline (Prompt 33, `33_AI_DATA_ANALYST_AGENT_CONTRACT`).

    understand -> execute <-> analyze -> recommend -> explain -> END

- understand: `check_input` on the question, then one bounded LLM call that
  decomposes it (`agent.analyst.planner`). Any failure falls back to the
  single original question, which is the plain Text-to-SQL path.
- execute: runs ONE sub-question through `agent.graph.run_agent`, the full
  governed pipeline (input guard, schema retrieval, SQL validation, RBAC and
  restricted-column gate, row cap, timeout). The analyst never generates or
  executes SQL itself.
- analyze: reads the finished sub-questions' evidence for anomalies and
  deterministically queues one follow-up per anomalous period. Explicit
  templates only, so an LLM never picks what to investigate next.
- recommend: merges the sub-runs' recommendations, dedupes, and links each
  to the evidence it cites.
- explain: a deterministic, evidence-linked report. Always runs, even after
  a budget stop, so a partial analysis is still reported honestly.

Every charged action is gated by `agent.analyst.budget.can_afford` before it
runs and charged by `spend` after. Explain and recommend are never charged,
so they always complete.

Trust boundaries:
- The question, planner output and every follow-up text are untrusted. Each
  one goes through `check_input` before a run, and then through the same
  governed pipeline as any other question. A planner or follow-up can ask for
  data the caller may not see, but `run_agent`'s authorization denies it there.
- Retrieved data is never fed back into an LLM prompt by this module. The
  report is built from fixed templates and typed values.
- The caller's roles and tenant are passed to every sub-run unchanged. Nothing
  is read from client input beyond the question and the conversation history.
"""

from __future__ import annotations

import logging
import re
import time
from functools import lru_cache
from typing import Any

from langgraph.graph import END, StateGraph

from agent.analyst.budget import (
    AnalystBudget,
    budget_from_dict,
    can_afford,
    new_usage,
    spend,
    usage_summary,
)
from agent.analyst.evidence import build_outcome, failed_outcome
from agent.analyst.planner import plan_subquestions
from agent.analyst.state import (
    AnalystState,
    EvidenceItem,
    RecommendationCandidate,
    Subquestion,
    SubquestionOrigin,
    TraceEntry,
)
from agent.graph import run_agent
from agent.input_guard import check_input, rejection_message
from agent.state import ConversationExchange
from config.settings import get_settings
from security.audit_log import reset_audit_tenant_id, set_audit_tenant_id

logger = logging.getLogger(__name__)

# A period label from the database is interpolated into a follow-up question,
# so it must be short and plain. Anything else is skipped, never sent. This
# keeps one row value from ever reading as an instruction.
_PERIOD_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-/.]{0,15}$")

_STATUS_NOTE = {
    "succeeded": "Completed.",
    "partial": "Partly completed. Some steps did not finish.",
    "insufficient_data": "Insufficient data. The governed queries returned no usable evidence.",
    "needs_clarification": "The question needs clarification before it can be answered.",
    "rejected": "The question was rejected by the input checks.",
    "failed": "The analysis could not be completed.",
}

_STOP_NOTE = {
    "timeout": "the time budget ran out",
    "step_budget_exhausted": "the step budget ran out",
    "subquery_budget_exhausted": "the sub-question budget ran out",
    "llm_budget_exhausted": "the planning budget ran out",
    "followup_budget_exhausted": "the follow-up budget ran out",
}


def _now() -> float:
    """The monotonic clock every deadline check reads. A module-level function
    so tests can drive the timeout deterministically."""
    return time.monotonic()


def _trace(state: AnalystState, stage: str, status: str, detail: str) -> list[TraceEntry]:
    """One trace entry, numbered after everything already recorded."""
    seq = len(state.get("trace") or []) + 1
    return [TraceEntry(seq=seq, stage=stage, status=status, detail=detail)]


def _budget(state: AnalystState) -> AnalystBudget:
    return budget_from_dict(state["budget"])


# --- Nodes -----------------------------------------------------------------


def understand_node(state: AnalystState) -> dict[str, Any]:
    """Checks the question, then decomposes it into sub-questions."""
    settings = get_settings()
    budget = _budget(state)
    now = _now()
    usage = state["usage"]

    stop = can_afford(budget, usage, now, "step")
    if stop:
        return {"stop_reason": stop, "trace": _trace(state, "understand", "stopped", stop)}
    usage = spend(usage, "step")

    guard = check_input(state["question"], settings.max_question_length)
    if not guard.passed:
        return {
            "usage": usage,
            "status": "rejected",
            # A failed check always carries a reason; the fallback only
            # satisfies the type checker and is never the one shown.
            "open_items": [rejection_message(guard.reason or "policy_refused", None)],
            "trace": _trace(state, "understand", "rejected", str(guard.reason)),
        }
    question = guard.cleaned_question

    if can_afford(budget, usage, now, "llm_call") is not None:
        texts, mode = [question], "fallback"
        usage_note = "planner skipped: LLM budget unavailable"
    else:
        usage = spend(usage, "llm_call")
        texts, mode = plan_subquestions(
            question, settings, state.get("model"), budget.max_subqueries
        )
        usage_note = f"planned by {mode}"

    subquestions: list[Subquestion] = []
    dropped = 0
    for text in texts:
        # Planner output is model text, so it is re-checked like any user
        # question. A failing item is counted and dropped, never echoed back.
        item_guard = check_input(text, settings.max_question_length)
        if not item_guard.passed:
            dropped += 1
            continue
        origin: SubquestionOrigin = "planner" if mode == "planner" else "user"
        subquestions.append(
            Subquestion(
                id=f"Q{len(subquestions) + 1}",
                text=item_guard.cleaned_question,
                origin=origin,
                status="pending",
                evidence_ids=[],
                parent_evidence_id=None,
            )
        )

    detail = f"{usage_note}; {len(subquestions)} sub-question(s)"
    if dropped:
        detail += f"; {dropped} item(s) dropped by input checks"
    open_items = (
        [] if not dropped else [f"{dropped} planned step(s) were dropped by the input checks."]
    )
    if not subquestions:
        return {
            "usage": usage,
            "planning_mode": mode,
            "status": "rejected",
            "open_items": open_items or ["No usable sub-question could be formed."],
            "trace": _trace(state, "understand", "rejected", "no usable sub-question"),
        }
    return {
        "usage": usage,
        "planning_mode": mode,
        "subquestions": subquestions,
        "open_items": open_items,
        "trace": _trace(state, "understand", "ok", detail),
    }


def execute_node(state: AnalystState) -> dict[str, Any]:
    """Runs the next pending sub-question through the governed pipeline."""
    budget = _budget(state)
    now = _now()
    usage = state["usage"]
    pending = next((sq for sq in state.get("subquestions", []) if sq["status"] == "pending"), None)
    if pending is None:
        return {}

    stop = can_afford(budget, usage, now, "step") or can_afford(budget, usage, now, "subquery")
    if stop:
        return {"stop_reason": stop, "trace": _trace(state, "execute", "stopped", stop)}
    usage = spend(spend(usage, "step"), "subquery")

    next_index = len(state.get("evidence") or []) + 1
    try:
        final_state = run_agent(
            question=pending["text"],
            conversation_history=list(state.get("conversation_history") or []),
            enable_insight=False,
            caller_roles=tuple(state.get("caller_roles") or ()),
            model=state.get("model"),
            tenant_id=state.get("tenant_id"),
            forecast_horizon=None,
        )
        outcome = build_outcome(final_state, pending["id"], next_index, pending["text"])
    except Exception:
        # The sub-run's own exceptions were already handled inside
        # run_agent, so anything reaching here is unexpected. It is logged in
        # full server-side and the analysis continues with the other steps.
        logger.exception("Analyst sub-question %s raised; recording it as failed", pending["id"])
        outcome = failed_outcome(pending["text"])

    evidence_ids = [item["id"] for item in outcome["evidence"]]
    updated: list[Subquestion] = [
        (
            Subquestion(**{**sq, "status": outcome["status"], "evidence_ids": evidence_ids})
            if sq["id"] == pending["id"]
            else sq
        )
        for sq in state["subquestions"]
    ]
    candidates = [
        RecommendationCandidate(subquestion_id=pending["id"], recommendation=rec)
        for rec in outcome["recommendations"]
    ]
    return {
        "usage": usage,
        "subquestions": updated,
        "evidence": list(outcome["evidence"]),
        "recommendation_candidates": candidates,
        "open_items": list(outcome["open_items"]),
        "trace": _trace(
            state, "execute", outcome["status"], f'{pending["id"]}: {outcome["detail"]}'
        ),
    }


def analyze_node(state: AnalystState) -> dict[str, Any]:
    """Queues one follow-up for each anomalous period not yet investigated.

    The follow-up text is a fixed template, and the period label is the only
    value taken from the database, validated by `_PERIOD_PATTERN`. No LLM
    chooses what to investigate, so the bounded loop stays deterministic.
    """
    budget = _budget(state)
    now = _now()
    usage = state["usage"]

    stop = can_afford(budget, usage, now, "step")
    if stop:
        return {"stop_reason": stop, "trace": _trace(state, "analyze", "stopped", stop)}
    usage = spend(usage, "step")

    subquestions = list(state.get("subquestions") or [])
    evidence = list(state.get("evidence") or [])
    # A period is investigated once per analysis, however many times it
    # appears. A follow-up's own result can re-flag the same period under a
    # new evidence id, so dedupe on the period label as well as the evidence id.
    period_by_evidence = {item["id"]: item["period"] for item in evidence}
    already_followed_evidence = {
        sq["parent_evidence_id"] for sq in subquestions if sq["parent_evidence_id"] is not None
    }
    already_followed_periods = {
        period
        for sq in subquestions
        if sq["parent_evidence_id"] is not None
        and (period := period_by_evidence.get(sq["parent_evidence_id"])) is not None
    }
    leads: list[EvidenceItem] = []
    seen_periods: set[str] = set(already_followed_periods)
    for item in evidence:
        if not (
            item["kind"] == "analytics_finding"
            and item["finding_kind"] == "anomaly"
            and item["period"]
            and _PERIOD_PATTERN.match(item["period"])
        ):
            continue
        if item["id"] in already_followed_evidence or item["period"] in seen_periods:
            continue
        seen_periods.add(item["period"])
        leads.append(item)
    if not leads:
        return {"usage": usage, "trace": _trace(state, "analyze", "ok", "no anomaly leads")}

    new_items: list[Subquestion] = []
    open_items: list[str] = []
    stop_reason: str | None = None
    for lead in leads:
        # A follow-up is only queued if it can actually run. Otherwise the
        # analysis would promise an investigation it never performs, so the
        # gap is recorded explicitly instead.
        blocked = can_afford(budget, usage, now, "followup") or can_afford(
            budget, usage, now, "subquery"
        )
        if blocked:
            stop_reason = blocked
            open_items.append(
                f"Not run: investigation of period {lead['period']} ({_STOP_NOTE.get(blocked, blocked)})."
            )
            break
        text = f"Break down the results for the period {lead['period']} by category."
        if not check_input(text, get_settings().max_question_length).passed:
            continue
        usage = spend(usage, "followup")
        new_items.append(
            Subquestion(
                id=f"Q{len(subquestions) + len(new_items) + 1}",
                text=text,
                origin="investigation",
                status="pending",
                evidence_ids=[],
                parent_evidence_id=lead["id"],
            )
        )
    detail = f"{len(new_items)} follow-up(s) queued from {len(leads)} anomaly lead(s)"
    update: dict[str, Any] = {
        "usage": usage,
        "subquestions": subquestions + new_items,
        "open_items": open_items,
        "trace": _trace(state, "analyze", "stopped" if stop_reason else "ok", detail),
    }
    if stop_reason:
        update["stop_reason"] = stop_reason
    return update


def recommend_node(state: AnalystState) -> dict[str, Any]:
    """Dedupes the sub-runs' recommendations and links each to its evidence.

    Recommendations keep the truth level their engine assigned (always
    AI_INFERENCE from `recommendation.engine`). Nothing is upgraded. A
    recommendation whose cited claim is not among this report's evidence is
    marked as such, rather than being given evidence it does not have.
    """
    settings = get_settings()
    evidence = list(state.get("evidence") or [])
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for candidate in state.get("recommendation_candidates") or []:
        rec = candidate["recommendation"]
        claim = rec.get("claim") or {}
        claim_text = str(claim.get("value", ""))
        key = (str(rec.get("category")), str(rec.get("kind")), claim_text)
        if key in seen or not claim_text:
            continue
        seen.add(key)
        cited = {str(item.get("value", "")) for item in rec.get("evidence") or []}
        evidence_ids = [
            item["id"]
            for item in evidence
            if item["subquestion_id"] == candidate["subquestion_id"] and item["claim"] in cited
        ]
        limitations = list(rec.get("limitations") or [])
        if not evidence_ids:
            limitations.append("The evidence this estimate cites is not itemised in this report.")
        out.append(
            {
                "id": f"R{len(out) + 1}",
                "subquestion_id": candidate["subquestion_id"],
                "claim": claim_text,
                "truth_level": str(getattr(claim.get("level"), "value", claim.get("level"))),
                "category": str(rec.get("category")),
                "action": rec.get("action"),
                "rationale": rec.get("rationale"),
                "confidence": rec.get("confidence"),
                "evidence_ids": evidence_ids,
                "limitations": limitations,
            }
        )
        if len(out) >= settings.analyst_max_recommendations:
            break
    return {
        "recommendations": out,
        "trace": _trace(state, "recommend", "ok", f"{len(out)} recommendation(s)"),
    }


def explain_node(state: AnalystState) -> dict[str, Any]:
    """Writes the final status and the evidence-linked report. Deterministic."""
    subquestions = list(state.get("subquestions") or [])
    evidence = list(state.get("evidence") or [])
    recommendations = list(state.get("recommendations") or [])
    stop = state.get("stop_reason")
    now = _now()

    # Any sub-question that did not finish cleanly makes the whole analysis
    # partial, not succeeded. An empty result is a clean outcome, so it is not
    # in this set. It is reported through the insufficient-data open item.
    unfinished = {"pending", "failed", "blocked", "rate_limited", "needs_clarification", "rejected"}
    if state.get("status") == "rejected":
        status = "rejected"
    else:
        statuses = {sq["status"] for sq in subquestions}
        # "succeeded" is the only status that means a step produced usable
        # data. A zero-row result is a clean outcome, but it is not an answer,
        # so an analysis where every step came back empty is insufficient data.
        has_data = "succeeded" in statuses
        if "needs_clarification" in statuses and not has_data:
            status = "needs_clarification"
        elif stop or statuses & unfinished:
            status = "partial"
        elif not has_data:
            if subquestions and statuses <= {"failed", "blocked", "rate_limited", "rejected"}:
                status = "failed"
            else:
                status = "insufficient_data"
        else:
            status = "succeeded"

    extra_open: list[str] = []
    for sq in subquestions:
        if sq["status"] == "pending":
            reason = _STOP_NOTE.get(stop or "", "the analysis stopped")
            extra_open.append(f'Not run: "{sq["text"]}" ({reason}).')
    if stop:
        extra_open.append(f"The analysis stopped early because {_STOP_NOTE.get(stop, stop)}.")

    report = _render_report(
        question=state.get("question", ""),
        status=status,
        subquestions=subquestions,
        evidence=evidence,
        recommendations=recommendations,
        open_items=list(state.get("open_items") or []) + extra_open,
        planning_mode=state.get("planning_mode"),
        usage_view=usage_summary(state["usage"], now),
    )
    return {
        "status": status,
        "report_markdown": report,
        "open_items": extra_open,
        "trace": _trace(state, "explain", "ok", status),
    }


def _render_report(
    *,
    question: str,
    status: str,
    subquestions: list[Subquestion],
    evidence: list[EvidenceItem],
    recommendations: list[dict],
    open_items: list[str],
    planning_mode: str | None,
    usage_view: dict,
) -> str:
    """The user-facing report, from fixed templates and typed values only.

    Labels separate what the database returned ("Observed") from what the
    analyst suggests ("AI estimate"), so a reader never mistakes one for the
    other. No value here is a number the analyst computed itself.
    """
    lines: list[str] = [
        f"# Analysis: {question}",
        "",
        f"**Status:** {_STATUS_NOTE.get(status, status)}",
        "",
    ]
    lines.append(
        f"Planning: {planning_mode or 'n/a'}. "
        f"Steps: {usage_view['steps']}, sub-questions: {usage_view['subqueries']}, "
        f"follow-ups: {usage_view['followups']}, elapsed: {usage_view['elapsed_seconds']}s."
    )
    lines += ["", "## Steps"]
    for sq in subquestions:
        origin = {
            "user": "your question",
            "planner": "AI decomposition",
            "investigation": "AI-suggested follow-up",
        }[sq["origin"]]
        lines.append(f'- {sq["id"]} ({origin}): {sq["text"]} -- {sq["status"]}')
    lines += ["", "## Observed (database facts)"]
    observed = [item for item in evidence if item["truth_level"] == "DATABASE_FACT"]
    lines += [
        f"- {item['id']} [{item['subquestion_id']}]: {item['claim']}" for item in observed
    ] or ["- None."]
    lines += ["", "## AI estimates (inference, not observed fact)"]
    if recommendations:
        for rec in recommendations:
            basis = ", ".join(rec["evidence_ids"]) or "no itemised evidence"
            confidence = (
                "" if rec["confidence"] is None else f" (confidence {rec['confidence']:.2f})"
            )
            lines.append(f"- {rec['id']}: {rec['claim']}{confidence}. Based on: {basis}.")
    else:
        lines.append("- None.")
    lines += ["", "## Open items"]
    lines += [f"- {item}" for item in open_items] or ["- None."]
    return "\n".join(lines)


# --- Routing ---------------------------------------------------------------


def route_after_understand(state: AnalystState) -> str:
    if state.get("stop_reason") or state.get("status") == "rejected":
        return "explain"
    return (
        "execute"
        if any(sq["status"] == "pending" for sq in state.get("subquestions", []))
        else "explain"
    )


def route_after_execute(state: AnalystState) -> str:
    if state.get("stop_reason"):
        return "recommend"
    if any(sq["status"] == "pending" for sq in state.get("subquestions", [])):
        return "execute"
    return "analyze"


def route_after_analyze(state: AnalystState) -> str:
    if state.get("stop_reason"):
        return "recommend"
    return (
        "execute"
        if any(sq["status"] == "pending" for sq in state.get("subquestions", []))
        else "recommend"
    )


# --- Graph -----------------------------------------------------------------


@lru_cache(maxsize=1)
def build_analyst_graph():
    """Compiles the analyst graph once per process. Stateless, like
    `agent.graph.build_graph`: every per-run value lives in the input state."""
    graph = StateGraph(AnalystState)
    graph.add_node("understand", understand_node)
    graph.add_node("execute", execute_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("recommend", recommend_node)
    graph.add_node("explain", explain_node)
    graph.set_entry_point("understand")
    graph.add_conditional_edges(
        "understand", route_after_understand, {"execute": "execute", "explain": "explain"}
    )
    graph.add_conditional_edges(
        "execute",
        route_after_execute,
        {"execute": "execute", "analyze": "analyze", "recommend": "recommend"},
    )
    graph.add_conditional_edges(
        "analyze", route_after_analyze, {"execute": "execute", "recommend": "recommend"}
    )
    graph.add_edge("recommend", "explain")
    graph.add_edge("explain", END)
    return graph.compile()


def run_analysis(
    question: str,
    *,
    budget: AnalystBudget,
    caller_roles: tuple[str, ...] = (),
    caller_subject: str | None = None,
    tenant_id: str | None = None,
    model: str | None = None,
    conversation_history: list[ConversationExchange] | None = None,
) -> AnalystState:
    """Runs one analysis end to end. The single entry point the API calls.

    The caller has already authorized the request (ASK and EXECUTE_SQL) and
    sanitized the conversation history. Here the tenant is bound for audit
    logging, the same way `agent.graph.run_agent` binds it, and reset in
    `finally`.
    """
    initial: AnalystState = {
        "question": question,
        "caller_roles": tuple(caller_roles),
        "caller_subject": caller_subject,
        "tenant_id": tenant_id,
        "model": model,
        "conversation_history": list(conversation_history or []),
        "budget": budget.to_dict(),
        "usage": new_usage(_now()),
        "subquestions": [],
        "evidence": [],
        "recommendation_candidates": [],
        "open_items": [],
        "trace": [],
        "stop_reason": None,
        "status": None,
        "report_markdown": None,
        "recommendations": [],
        "planning_mode": None,
    }
    token = set_audit_tenant_id(tenant_id)
    try:
        final = build_analyst_graph().invoke(
            initial, config={"recursion_limit": budget.max_steps * 3 + 20}
        )
    finally:
        reset_audit_tenant_id(token)
    logger.info(
        "Analyst run finished: status=%s stop_reason=%s subqueries=%d evidence=%d",
        final.get("status"),
        final.get("stop_reason"),
        len(final.get("subquestions", [])),
        len(final.get("evidence", [])),
    )
    return final
