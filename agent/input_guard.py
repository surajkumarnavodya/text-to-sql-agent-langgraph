"""Pre-flight gate for user questions: sanitize, then decide if it's even worth
a schema-retrieval + LLM generation cycle at all.

Two cheap, regex-only checks run before anything expensive (`agent.nodes.
sanitize_input_node` is the graph's entry point, ahead of even
`classify_followup_node`):

1. **Injection-pattern detection** -- phrases that try to redirect the
   model's behavior ("ignore previous instructions", "you are now in
   developer mode", "print your system prompt", ...). This is deliberately
   NOT the primary defense (see CLAUDE.md's constraint against a keyword
   blocklist as a security boundary): a determined rephrasing can dodge any
   fixed pattern list. It exists to catch the obvious, common cases cheaply
   -- without spending a generation cycle -- and to produce a clean,
   consistent fallback message. The *real* guarantee against prompt
   injection is structural and layered elsewhere: `agent.llm_client`'s
   system prompt instructs the model to treat the entire question as
   untrusted data to convert, never as instructions, no matter what slips
   past this filter; and even if the model were somehow fully hijacked, it
   can still only ever produce text, which `agent.sql_validator` then holds
   to a strict SELECT-only allowlist before anything executes. This filter
   is a fast, cheap first layer -- not the last line of defense.

2. **Relevance/off-topic classification** -- does this look like a
   database question at all, or a request to do something else entirely
   (write a poem, solve math, roleplay, general trivia)? Scoped
   deliberately narrow: it only fires on positive, low-false-positive
   signals (a request to write/compose/translate/roleplay, a recognizable
   trivia-question shape, pure gibberish, or empty input) -- it does NOT
   try to guess topic from the absence of database-sounding vocabulary,
   since that would misfire on legitimate follow-ups like "just show the
   top 3 of those" (see `agent.followup`, which already owns that
   territory). Anything ambiguous enough to not match one of these signals
   passes through to normal generation, backed by two further layers if it
   turns out to be off-topic anyway: the model's own system-prompt
   instruction to refuse a non-SQL request, and the existing "couldn't find
   relevant data" fallback if schema retrieval comes up empty.

Every rejection is logged through this module's own logger (`agent.
input_guard` -- a distinct category from `agent.nodes`'/`agent.llm_client`'s
normal generation/retry logs, so these are easy to filter/route/alert on
separately) at WARNING, with the raw question capped via `security.
sanitization.truncate_for_log` -- an attacker-controlled string is exactly
the kind of thing that could otherwise be used to flood logs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from security.sanitization import normalize_text, truncate_for_log

logger = logging.getLogger(__name__)

RejectionReason = Literal["too_long", "empty", "injection_detected", "off_topic"]

# Phrases that attempt to redirect the model's behavior rather than ask a
# database question. See module docstring: a fast, cheap first layer, not
# the security boundary itself.
_INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore_instructions": re.compile(
        # Either word order: "ignore previous instructions" (qualifier
        # before the noun) and "disregard the rules above" (qualifier
        # after) are both natural English and both attempts at the same
        # thing -- matching only one order left the other undetected.
        r"\b(ignore|disregard|forget)\b(?:\s+\w+){0,3}\s+"
        r"\b(previous|prior|above|earlier|all|these|those)\b(?:\s+\w+){0,2}\s+"
        r"\b(instructions?|rules?|prompt|directives?)\b"
        r"|"
        r"\b(ignore|disregard|forget)\b(?:\s+\w+){0,3}\s+"
        r"\b(instructions?|rules?|prompt|directives?)\b(?:\s+\w+){0,3}\s+"
        r"\b(previous|prior|above|earlier|all|these|those)\b",
        re.IGNORECASE,
    ),
    "role_override": re.compile(
        r"\b(you are now|act as|pretend (to be|you are)|from now on you are|"
        r"switch to|enable)\b(?:\s+\w+){0,4}\s+\b(developer|debug|admin|god|dan|"
        r"unrestricted|jailbreak)\b(?:\s+mode)?|"
        r"\b(developer|debug|admin|god)\s+mode\b",
        re.IGNORECASE,
    ),
    "reveal_prompt": re.compile(
        r"\b(print|show|reveal|repeat|output|display|leak)\b(?:\s+\w+){0,3}\s+"
        r"\b(your\s+)?(system\s+prompt|instructions|configuration|"
        r"initial\s+prompt|prompt\s+above)\b",
        re.IGNORECASE,
    ),
    "reveal_internal_files": re.compile(
        r"\b(table_descriptions?\.ya?ml|\.env\b|source\s+code|api\s+key|" r"connection\s+string)\b",
        re.IGNORECASE,
    ),
    "fake_role_marker": re.compile(r"^\s*(system|assistant)\s*:", re.IGNORECASE | re.MULTILINE),
    "new_instructions_marker": re.compile(
        r"\bnew\s+instructions?\s*:|^\s*###|\[/?(system|inst)\]", re.IGNORECASE
    ),
}

# Positive signals that this is a request to do something other than
# generate SQL against the connected database. Intentionally scoped to
# intent/request shapes, never to the *absence* of database vocabulary --
# see module docstring for why that distinction matters.
_OFF_TOPIC_PATTERNS: dict[str, re.Pattern[str]] = {
    "creative_writing": re.compile(
        r"\b(write|compose|generate)\b(?:\s+\w+){0,3}\s+"
        r"\b(a\s+)?(poem|story|song|haiku|essay|joke|lyrics|limerick)\b",
        re.IGNORECASE,
    ),
    "roleplay": re.compile(
        r"\b(role-?play|pretend (to be|you are)|act as (a|an)\b)", re.IGNORECASE
    ),
    "translation": re.compile(
        # \S+ (any non-whitespace), not \w+ -- a quoted phrase like
        # "translate 'hello' to Spanish" has punctuation immediately
        # around the word being translated, which \w+ can't span.
        r"\btranslate\b(?:\s+\S+){0,10}\s+\b(to|into)\b",
        re.IGNORECASE,
    ),
    "general_trivia": re.compile(
        r"\b(capital of|president of|prime minister of|population of|"
        r"who (invented|discovered|wrote|painted|directed)|"
        r"what year (was|did)|largest (planet|country|ocean|continent)|"
        r"speed of light|how far is\b)",
        re.IGNORECASE,
    ),
    "math_request": re.compile(
        r"^\s*(solve|calculate|compute)\b|^[\s\d+\-*/().]+=?\s*$", re.IGNORECASE
    ),
    "tell_joke": re.compile(r"\btell (me )?a joke\b", re.IGNORECASE),
    "code_request": re.compile(
        r"\b(write|generate)\b(?:\s+\w+){0,3}\s+\b(python|javascript|java|c\+\+|code|function|script)\b",
        re.IGNORECASE,
    ),
}

_WORD_RE = re.compile(r"[a-zA-Z]{2,}")


def _looks_like_gibberish(text: str) -> bool:
    """True if `text` contains no recognizable word-like tokens at all.

    Deliberately narrow (>=2 consecutive letters counts as "word-like") so
    this never fires on a legitimate short follow-up fragment ("why",
    "more") -- those are `agent.followup`'s territory (handled as
    "ambiguous", with its own clarification message), not this module's.
    This only catches genuine noise: random symbols/digits with no
    alphabetic content at all.
    """
    return not _WORD_RE.search(text)


# User-facing text for every rejection reason -- see CLAUDE.md's Part 3:
# consistent, non-technical, never confirms exactly what was detected (the
# "injection_detected" message in particular is deliberately generic, so it
# gives an attacker no signal to iterate against, and doesn't alarm a
# legitimate user who just phrased something unusually).
_MESSAGES: dict[RejectionReason, str] = {
    "too_long": (
        "That question is a bit long or complex for me to process -- try "
        "breaking it into a shorter, more specific question."
    ),
    "empty": "I didn't receive a question -- try asking something like 'total sales by year.'",
    "injection_detected": (
        "I couldn't process that question. Try rephrasing it as a direct "
        "question about your data."
    ),
    "off_topic": (
        "I can only answer questions about {db_name}. Try asking something "
        "like 'total sales by year.'"
    ),
}

# Reused by agent.nodes when schema retrieval genuinely finds nothing
# relevant -- not a RejectionReason (that path still goes through normal
# generation/retry, just ends with this as the final message; see
# CLAUDE.md's "existing behavior, keep as is").
NO_RELEVANT_DATA_MESSAGE = "I couldn't find data related to that in this database."


@dataclass(frozen=True)
class GuardResult:
    """Outcome of `check_input`.

    Attributes:
        passed: Whether the question may proceed to follow-up classification
            and generation.
        cleaned_question: The normalized (NFKC + confusables-folded +
            control-character-stripped) question text. Always set, even on
            rejection, so callers never need to re-normalize.
        reason: Why the question was rejected (None if passed).
        matched_patterns: Which named pattern(s) fired -- for logging only,
            never shown to the user (see `_MESSAGES`).
    """

    passed: bool
    cleaned_question: str
    reason: RejectionReason | None = None
    matched_patterns: tuple[str, ...] = ()


def rejection_message(reason: RejectionReason, db_name: str | None) -> str:
    """Renders the standardized user-facing message for a rejection reason."""
    template = _MESSAGES[reason]
    return template.format(db_name=db_name or "this database")


def check_input(question: str, max_length: int) -> GuardResult:
    """Sanitizes and gate-checks a raw question before it reaches the agent.

    Order matters: the length check runs on the *raw* input, before any
    normalization work is done on it -- a cheap rejection for an absurdly
    long string shouldn't first pay the cost of Unicode-normalizing all of
    it. Everything after that operates on the normalized text.

    Args:
        question: The raw, as-typed user question.
        max_length: Maximum accepted raw length (`Settings.max_question_length`).

    Returns:
        A `GuardResult`. `passed=True` means safe to hand to
        `agent.followup.classify_followup` next.
    """
    if len(question) > max_length:
        logger.warning(
            "[input_guard] rejected reason=too_long length=%d max=%d excerpt=%r",
            len(question),
            max_length,
            truncate_for_log(question),
        )
        return GuardResult(passed=False, cleaned_question="", reason="too_long")

    cleaned = normalize_text(question)
    if not cleaned:
        logger.warning("[input_guard] rejected reason=empty")
        return GuardResult(passed=False, cleaned_question="", reason="empty")

    injection_matches = tuple(
        name for name, pattern in _INJECTION_PATTERNS.items() if pattern.search(cleaned)
    )
    if injection_matches:
        logger.warning(
            "[input_guard] rejected reason=injection_detected patterns=%s excerpt=%r",
            injection_matches,
            truncate_for_log(cleaned),
        )
        return GuardResult(
            passed=False,
            cleaned_question=cleaned,
            reason="injection_detected",
            matched_patterns=injection_matches,
        )

    offtopic_matches = tuple(
        name for name, pattern in _OFF_TOPIC_PATTERNS.items() if pattern.search(cleaned)
    )
    is_gibberish = _looks_like_gibberish(cleaned)
    if offtopic_matches or is_gibberish:
        logger.warning(
            "[input_guard] rejected reason=off_topic patterns=%s gibberish=%s excerpt=%r",
            offtopic_matches,
            is_gibberish,
            truncate_for_log(cleaned),
        )
        return GuardResult(
            passed=False,
            cleaned_question=cleaned,
            reason="off_topic",
            matched_patterns=offtopic_matches,
        )

    return GuardResult(passed=True, cleaned_question=cleaned)
