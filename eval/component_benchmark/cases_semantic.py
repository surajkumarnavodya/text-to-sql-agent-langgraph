"""Semantic-layer cases: governed-metric rendering determinism.

`agent.llm_client._build_mandatory_metrics_block` is the literal mechanism
behind Prompt 10's acceptance criterion -- "equivalent KPI questions
consistently use the governed metric definition." It takes no `question`
parameter at all, so determinism is guaranteed by construction, not just
empirically likely; what these cases actually guard against is a future
edit silently changing that (e.g. switching to an f-string that threads in
something per-call, or dropping the approved expression on an unusual
input shape).
"""

from __future__ import annotations

from agent.llm_client import _build_mandatory_metrics_block
from eval.component_benchmark.schema import ComponentCase

_REVENUE_METRIC = {
    "business_name": "Net Revenue",
    "approved_expression": "SUM(order_total) - SUM(refund_amount)",
    "text": "Net revenue is gross order total minus refunds.",
}
_CHURN_METRIC = {
    "business_name": "Monthly Churn Rate",
    "approved_expression": "COUNT(CASE WHEN cancelled_this_month THEN 1 END) / COUNT(*)",
    "text": "The fraction of customers who cancelled this month.",
}


def _case_same_input_renders_identically() -> tuple[bool, str]:
    first = _build_mandatory_metrics_block([_REVENUE_METRIC])
    second = _build_mandatory_metrics_block([_REVENUE_METRIC])
    passed = first == second
    return passed, f"first_len={len(first)} second_len={len(second)} identical={passed}"


def _case_approved_expression_always_present_verbatim() -> tuple[bool, str]:
    rendered = _build_mandatory_metrics_block([_REVENUE_METRIC])
    passed = _REVENUE_METRIC["approved_expression"] in rendered
    return passed, f"expression_present={passed}"


def _case_multiple_metrics_each_rendered() -> tuple[bool, str]:
    rendered = _build_mandatory_metrics_block([_REVENUE_METRIC, _CHURN_METRIC])
    passed = (
        _REVENUE_METRIC["approved_expression"] in rendered
        and _CHURN_METRIC["approved_expression"] in rendered
        and _REVENUE_METRIC["business_name"] in rendered
        and _CHURN_METRIC["business_name"] in rendered
    )
    return passed, f"both_metrics_present={passed}"


def _case_empty_list_never_crashes_and_carries_no_expression() -> tuple[bool, str]:
    rendered = _build_mandatory_metrics_block([])
    passed = isinstance(rendered, str) and "approved expression" in rendered
    return passed, f"rendered_len={len(rendered)}"


def _case_missing_approved_expression_falls_back_to_text_not_blank() -> tuple[bool, str]:
    """A catalog entry without `approved_expression` set (shouldn't happen
    for a real governing metric, but the renderer must degrade safely, not
    silently produce an empty instruction for that metric)."""
    partial_metric = {"business_name": "Undefined Metric", "text": "fallback description"}
    rendered = _build_mandatory_metrics_block([partial_metric])
    passed = "fallback description" in rendered and "Undefined Metric" in rendered
    return passed, f"fallback_rendered={passed}"


def _case_always_framed_as_data_not_a_user_instruction() -> tuple[bool, str]:
    """The security framing itself (never silently dropped) -- the block
    must always carry the "DATA ... not instructions from the user" guard
    this codebase's injection-safety convention requires of every per-
    question prompt block."""
    rendered = _build_mandatory_metrics_block([_REVENUE_METRIC])
    passed = "not instructions from the user" in rendered
    return passed, f"injection_framing_present={passed}"


CASES: list[ComponentCase] = [
    ComponentCase(
        "same_input_renders_identically",
        "The same governing metric renders byte-identical output on repeated calls.",
        _case_same_input_renders_identically,
    ),
    ComponentCase(
        "approved_expression_always_present_verbatim",
        "The approved expression appears verbatim in the rendered block.",
        _case_approved_expression_always_present_verbatim,
    ),
    ComponentCase(
        "multiple_metrics_each_rendered",
        "Every governing metric in a multi-metric list is rendered, not just the first.",
        _case_multiple_metrics_each_rendered,
    ),
    ComponentCase(
        "empty_list_never_crashes",
        "An empty governing-metrics list renders a well-formed block, never raises.",
        _case_empty_list_never_crashes_and_carries_no_expression,
    ),
    ComponentCase(
        "missing_approved_expression_falls_back_to_text",
        "A metric entry with no approved_expression falls back to its text, not a blank line.",
        _case_missing_approved_expression_falls_back_to_text_not_blank,
    ),
    ComponentCase(
        "always_framed_as_data_not_a_user_instruction",
        "The rendered block always carries the injection-safe 'DATA, not instructions' framing.",
        _case_always_framed_as_data_not_a_user_instruction,
    ),
]
