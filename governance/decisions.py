"""The vocabulary every governance check speaks: one decision per request.

A request ends in exactly one of these states. Only `REFUSE` and
`REQUIRE_AUTHORIZATION` stop the request before any data is touched; the
others let it continue, optionally with redaction or a visible notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GovernanceDecision(StrEnum):
    ALLOW = "allow"
    ALLOW_WITH_REDACTION = "allow_with_redaction"
    ALLOW_AGGREGATED = "allow_aggregated"
    ALLOW_WITH_WARNING = "allow_with_warning"
    REQUIRE_AUTHORIZATION = "require_authorization"
    REFUSE = "refuse"


#: Decisions that stop a request before schema retrieval, SQL generation, or execution.
BLOCKING_DECISIONS: frozenset[GovernanceDecision] = frozenset(
    {GovernanceDecision.REQUIRE_AUTHORIZATION, GovernanceDecision.REFUSE}
)

#: Domain label for a request no domain-specific rule matched.
GENERAL_DOMAIN = "general"


@dataclass(frozen=True)
class GovernanceVerdict:
    """The outcome of evaluating one request.

    `message` is the user-facing explanation. It is a fixed, reviewed string
    per rule and never echoes the question back, so the reply cannot reveal
    what a rule matched on. `category` names the rule for audit logs only.
    """

    decision: GovernanceDecision
    domain: str = GENERAL_DOMAIN
    category: str = ""
    message: str = ""

    @property
    def is_blocking(self) -> bool:
        return self.decision in BLOCKING_DECISIONS


ALLOW_VERDICT = GovernanceVerdict(decision=GovernanceDecision.ALLOW)
