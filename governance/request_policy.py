"""Request-level governance: decides whether a question may be answered at all.

Runs on the already-normalized question, before schema retrieval, SQL
generation, or any source is called. It is deliberately deterministic, not
an LLM judgment, so a refusal does not depend on the model choosing to refuse.

Authorization comes only from the caller's roles (`agent.authz`). Text in
the question that claims authority ("this is authorized", "pretend I am an
admin") is never consulted, so it cannot unlock anything. Rules are checked
in order and the first match decides. Rules marked with a `permission` only
block callers that lack it.

Scope: these rules cover high-risk request shapes across the business
domains. They are not a complete classifier. Result-level protection
(`governance.result_policy`) and SQL-level restricted-column checks
(`agent.sql_validator`) still apply to anything these rules let through.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agent.authz import Permission, has_role_permission
from governance.decisions import (
    ALLOW_VERDICT,
    GovernanceDecision,
    GovernanceVerdict,
)


@dataclass(frozen=True)
class _Rule:
    domain: str
    category: str
    pattern: re.Pattern[str]
    decision: GovernanceDecision
    message: str
    #: If set, a caller holding this permission is not blocked by this rule.
    permission: Permission | None = None


_VERB = r"(?:show|give|reveal|list|tell|print|display|dump|export|share|send|what\s*(?:is|are|'s))"

_PROMPT_BYPASS_MESSAGE = (
    "Access rules can't be changed from a question. What you can see depends on "
    "your account's permissions."
)
_SECRETS_MESSAGE = (
    "I can't show credentials, secrets or security configuration, even when they "
    "appear in the data."
)
_PAYMENT_MESSAGE = (
    "I can't expose full account numbers, card details, PINs or other payment "
    "credentials. I can provide aggregate banking analytics instead."
)
_PRESCRIPTION_MESSAGE = (
    "I can provide authorized healthcare analytics, but I can't provide individual "
    "prescriptions, dosages or treatment decisions. Please consult a qualified "
    "healthcare professional."
)
_LEGAL_MESSAGE = (
    "Privileged legal content and legal advice aren't available here. Please consult "
    "qualified counsel."
)
_FRAUD_LABEL_MESSAGE = (
    "Fraud analytics can flag a transaction that matches configured risk indicators. "
    "I can't determine that a person committed fraud."
)
_CREDIT_DECISION_MESSAGE = (
    "I can't make or recommend an individual loan or credit decision. I can provide "
    "aggregate portfolio analytics."
)
_EMPLOYMENT_ACTION_MESSAGE = (
    "I can't recommend individual employment actions such as termination, demotion or "
    "discipline. I can show aggregate workforce trends."
)
_RANKING_MESSAGE = (
    "I can't rank or label individual employees using confidential performance "
    "information. I can provide aggregate performance trends by department, role or "
    "other authorized dimensions."
)
_APPRAISAL_MESSAGE = (
    "Individual appraisal and feedback information is restricted. I can provide "
    "aggregate performance analytics by department or role."
)
_COMPENSATION_MESSAGE = (
    "Salary information is restricted. I can provide authorized aggregated "
    "compensation analytics by department, grade or role."
)

_RULES: tuple[_Rule, ...] = (
    _Rule(
        domain="governance",
        category="prompt_bypass",
        pattern=re.compile(
            r"\b(?:ignore|disable|bypass|turn\s+off|override|skip)\s+"
            r"(?:all\s+|the\s+|your\s+|any\s+|these\s+)?"
            r"(?:privacy|safety|governance|security|policy|policies|rules|restrictions|"
            r"guardrails|masking|redaction)\b"
            r"|\bpretend\s+(?:that\s+)?(?:i\s+am|i'm|you\s+are)\s+(?:an?\s+)?"
            r"(?:admin|administrator|superuser|root)\b"
            r"|\bshow\s+me\s+(?:everything|all\s+(?:the\s+)?data)\s+"
            r"(?:without|including\s+(?:the\s+)?(?:restricted|sensitive|private))",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REFUSE,
        message=_PROMPT_BYPASS_MESSAGE,
    ),
    _Rule(
        domain="it_operations",
        category="secrets",
        pattern=re.compile(
            rf"\b{_VERB}\b[^.?!]{{0,40}}\b(?:passwords?|api[\s_-]?keys?|access[\s_-]?tokens?|"
            r"private[\s_-]?keys?|secret[\s_-]?keys?|connection[\s_-]?strings?|"
            r"db[\s_-]?credentials?|credentials?|otp|one[\s-]time\s+(?:codes?|passwords?))\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REFUSE,
        message=_SECRETS_MESSAGE,
    ),
    _Rule(
        domain="bfsi",
        category="payment_credentials",
        pattern=re.compile(
            rf"\b{_VERB}\b[^.?!]{{0,40}}\b(?:card\s+numbers?|full\s+account\s+numbers?|"
            r"account\s+numbers?|cvv|cvc|pins?|card\s+details|bank\s+details|"
            r"bank\s+credentials|iban|routing\s+numbers?)\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REFUSE,
        message=_PAYMENT_MESSAGE,
    ),
    _Rule(
        domain="healthcare",
        category="individual_prescription",
        pattern=re.compile(
            r"\bprescri(?:be|bing)\s+(?:a\s+|an\s+|the\s+|some\s+)?"
            r"(?:medicine|medication|drug|dose|dosage|treatment|antibiotic|painkiller|opioid)s?\b"
            r"|\b(?:dos(?:e|age)|medication|medicine|drug)\s+(?:for|of)\s+"
            r"(?:this|that|the|a|my)\s+patient\b"
            r"|\bwhich\s+(?:medicine|medication|drug|treatment)s?\s+should\s+"
            r"(?:this|the|a|my|he|she|they)\b"
            r"|\bdiagnos(?:e|is)\s+(?:this|the|a|my)\s+patient\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REFUSE,
        message=_PRESCRIPTION_MESSAGE,
    ),
    _Rule(
        domain="legal",
        category="privileged_content",
        pattern=re.compile(
            r"\b(?:attorney[\s-]client|privileged\s+(?:legal\s+)?(?:advice|communications?|"
            r"documents?|information)|litigation\s+strategy|legal\s+(?:advice|opinions?)|"
            r"settlement\s+strategy|is\s+(?:this|the|our)\s+company\s+liable)\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REFUSE,
        message=_LEGAL_MESSAGE,
    ),
    _Rule(
        domain="bfsi",
        category="fraud_label",
        pattern=re.compile(
            r"\b(?:is|are|was|were)\s+(?:this|that|the|a|these)\s+"
            r"(?:customer|user|account|person|client|applicant|individual)s?\s+"
            r"(?:a\s+|an\s+)?(?:fraud(?:ster|sters|ulent)?|criminal|thief|"
            r"money\s+launderer|launderer)\b"
            r"|\b(?:label|call|mark|brand|tag)\s+(?:this|that|the|a)\s+"
            r"(?:customer|user|account|person|client)\s+(?:as\s+)?(?:a\s+|an\s+)?"
            r"(?:fraud\w*|criminal|thief)\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REFUSE,
        message=_FRAUD_LABEL_MESSAGE,
    ),
    _Rule(
        domain="bfsi",
        category="individual_credit_decision",
        pattern=re.compile(
            r"\b(?:approve|reject|deny|decline|grant|block)\s+"
            r"(?:this|that|the|a|his|her|their)\s+(?:loan|credit|application|mortgage|limit)s?\b"
            r"|\bshould\s+(?:we|i)\s+(?:approve|reject|deny|decline|grant)\s+"
            r"(?:this|that|the|a)\s+(?:loan|credit|applicant|customer|application|mortgage)\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REFUSE,
        message=_CREDIT_DECISION_MESSAGE,
    ),
    _Rule(
        domain="hr",
        category="individual_employment_action",
        pattern=re.compile(
            r"\b(?:fire|terminate|dismiss|sack|let\s+go|demote|discipline|promote)\s+"
            r"(?:this|that|him|her|them)\b"
            r"|\b(?:fire|terminate|dismiss|sack|demote|discipline|promote)\s+"
            r"(?:an?\s+|the\s+)?(?:employee|staff|worker|manager)s?\b"
            r"|\bshould\s+(?:we|i)\s+(?:fire|terminate|dismiss|demote|discipline)\b"
            r"|\bwho\s+should\s+(?:we|i)\s+(?:fire|terminate|dismiss|let\s+go)\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REFUSE,
        message=_EMPLOYMENT_ACTION_MESSAGE,
    ),
    _Rule(
        domain="hr",
        category="individual_ranking",
        pattern=re.compile(
            r"\b(?:worst|best|bottom|poorest|lowest|least)\s+"
            r"(?:performing\s+|performer\s+)?(?:employee|staff|worker|performer)s?\b"
            r"|\bwho\s+is\s+the\s+(?:worst|best|bottom|least|lowest)\b"
            r"|\brank\s+(?:the\s+)?(?:employees?|staff|workers?|people)\s+"
            r"(?:by|on|based\s+on)\s+(?:performance|rating|appraisal|salary|pay|score)\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REFUSE,
        message=_RANKING_MESSAGE,
    ),
    _Rule(
        domain="hr",
        category="individual_appraisal",
        pattern=re.compile(
            r"\b(?:appraisal|performance\s+rating|manager\s+feedback|performance\s+review|"
            r"appraisal\s+comments?|disciplinary|promotion\s+recommendation)s?\b.{0,60}"
            r"\b(?:(?:this|that|a|the|one|his|her|their|specific)\s+(?:employee|staff|person|worker)"
            r"|employee\s+(?:id\s*)?#?\d+|him|her|them)\b"
            r"|\b(?:this|that|a|specific)\s+(?:employee|staff|worker)'?s?\s+"
            r"(?:performance|rating|appraisal|feedback|review|disciplinary)\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REQUIRE_AUTHORIZATION,
        message=_APPRAISAL_MESSAGE,
        permission=Permission.VIEW_RESTRICTED_COLUMNS,
    ),
    _Rule(
        domain="hr",
        category="individual_compensation",
        pattern=re.compile(
            r"\b(?:my|his|her|their)\s+(?:salary|salaries|pay|compensation|payslip)\b"
            r"|\b(?:salary|salaries|compensation|pay|payslip|earnings?)\b.{0,60}"
            r"\b(?:(?:this|that|a|the|one|his|her|their|specific|my)\s+(?:employee|staff|person|worker|manager)"
            r"|employee\s+(?:id\s*)?#?\d+|him|her|them|me)\b"
            r"|\bhow\s+much\s+(?:does|did|is|was)\s+(?:this|that|he|she|they|my)\b.{0,30}"
            r"\b(?:earn|make|paid|get)\b",
            re.IGNORECASE,
        ),
        decision=GovernanceDecision.REQUIRE_AUTHORIZATION,
        message=_COMPENSATION_MESSAGE,
        permission=Permission.VIEW_RESTRICTED_COLUMNS,
    ),
)


def evaluate_request(question: str, roles: Sequence[str]) -> GovernanceVerdict:
    """Returns the governance verdict for one already-normalized question.

    `roles` is the caller's role tuple (`AgentState["caller_roles"]`). An
    unrecognized role grants nothing (see `agent.authz`), so a permission-gated
    rule still blocks such a caller.
    """
    for rule in _RULES:
        if not rule.pattern.search(question):
            continue
        if rule.permission is not None and has_role_permission(roles, rule.permission):
            continue
        return GovernanceVerdict(
            decision=rule.decision,
            domain=rule.domain,
            category=rule.category,
            message=rule.message,
        )
    return ALLOW_VERDICT
