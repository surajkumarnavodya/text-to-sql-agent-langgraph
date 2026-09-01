"""Classifies a question as standalone, a follow-up, or ambiguous.

Cheap, regex-only heuristic -- deliberately not an LLM call -- run before
schema retrieval or generation (see `agent.nodes.classify_followup_node`),
so a standalone question pays zero extra cost and an ambiguous one fails
fast instead of burning a schema-retrieval + generation cycle first.

The classifier reduces a question to two independent signals:

    referring_signal -- does the text point at something outside itself
        (a pronoun/demonstrative, or a discourse marker like "now"/"instead"
        that presupposes a prior turn)?
    has_subject -- once referring words and ordinary function words are
        stripped, is there still a real content word left (an actual topic,
        not just a bare number or connector)?

`referring_signal` is the primary gate: if the question refers outward,
whether it's resolvable depends only on whether there *is* a prior exchange
to resolve it against (`has_history`) -- not on whether it also happens to
carry new content (a follow-up is allowed to both refer back *and* narrow
the question, e.g. "what about for 2013 instead?"). `has_subject` only
matters when there is no referring signal at all: it separates a genuine
standalone question from a bare, contentless fragment ("why", "more") that
can't stand on its own either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

FollowupClassification = Literal["standalone", "followup", "ambiguous"]

# Discourse markers that presuppose a prior turn when they lead the question.
_LEADING_RE = re.compile(
    r"^\s*(now|and|also|then|instead|just|only|what about|how about|same (?:thing|query|but))\b",
    re.IGNORECASE,
)

# Pronouns/demonstratives referring to something not named in this question.
_REFERRING_RE = re.compile(
    r"\b(that|those|this|these|it|them|the same|the previous|the last (?:one|query|result))\b",
    re.IGNORECASE,
)

# Refinement phrases that only make sense against a prior result.
_REFINEMENT_RE = re.compile(
    r"\b(top\s+\d+\s+of\s+(?:those|that|them)|break\s+(?:it|that|those)\s+down"
    r"|instead\s+of\s+that|for\s+(?:that|those)\s+instead)\b",
    re.IGNORECASE,
)

_SIGNAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "leading_discourse_marker": _LEADING_RE,
    "referring_pronoun": _REFERRING_RE,
    "refinement_phrase": _REFINEMENT_RE,
}

# Function words, referring words, and generic granularity words ("month",
# "year", ...) that don't count as introducing a new subject on their own --
# a question like "now break that down by month" is left with nothing after
# these are stripped, which is exactly the point: it has no topic of its own,
# only a shape to apply to whatever the prior question already established.
_SUBJECT_STOPWORDS: frozenset[str] = frozenset(
    {
        "show",
        "list",
        "give",
        "get",
        "find",
        "tell",
        "please",
        "the",
        "a",
        "an",
        "of",
        "for",
        "by",
        "in",
        "on",
        "at",
        "to",
        "and",
        "or",
        "now",
        "then",
        "also",
        "just",
        "only",
        "instead",
        "same",
        "again",
        "more",
        "that",
        "those",
        "this",
        "these",
        "it",
        "them",
        "previous",
        "last",
        "one",
        "query",
        "result",
        "results",
        "about",
        "how",
        "what",
        "whats",
        "which",
        "who",
        "why",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "we",
        "you",
        "us",
        "our",
        "me",
        "can",
        "could",
        "would",
        "break",
        "down",
        "top",
        "out",
        "with",
        "from",
        "so",
        "too",
        "month",
        "months",
        "quarter",
        "quarters",
        "year",
        "years",
        "week",
        "weeks",
        "day",
        "days",
        "date",
        "dates",
    }
)

_TOKEN_RE = re.compile(r"[a-zA-Z']+|\d+")


@dataclass(frozen=True)
class ClassificationResult:
    """Outcome of classifying one question.

    Attributes:
        classification: "standalone" | "followup" | "ambiguous".
        referring_signal: Whether an outward-pointing reference was detected.
        has_subject: Whether real content remains after stripping referring/
            function words.
        matched_patterns: Names of the signal patterns that matched, for
            logging (`agent.nodes.classify_followup_node` logs this so the
            decision is inspectable in the terminal like every other agent
            decision -- see CLAUDE.md's logging conventions).
    """

    classification: FollowupClassification
    referring_signal: bool
    has_subject: bool
    matched_patterns: tuple[str, ...]


def _detect_referring_signal(question: str) -> tuple[bool, tuple[str, ...]]:
    matched = tuple(name for name, pattern in _SIGNAL_PATTERNS.items() if pattern.search(question))
    return bool(matched), matched


def _has_subject(question: str) -> bool:
    tokens = _TOKEN_RE.findall(question.lower())
    if len(tokens) < 2:
        # A single-word question ("why", "more") never carries enough of its
        # own content to stand alone, regardless of what that word is.
        return False
    content_tokens = [
        t for t in tokens if t.isalpha() and len(t) >= 3 and t not in _SUBJECT_STOPWORDS
    ]
    return bool(content_tokens)


def classify_followup(question: str, has_history: bool) -> ClassificationResult:
    """Classifies `question` as standalone, a follow-up, or ambiguous.

    Args:
        question: The user's raw natural-language input for this turn.
        has_history: Whether there is at least one prior exchange in this
            session to potentially resolve a reference against.

    Returns:
        A `ClassificationResult`. See the module docstring for the
        referring_signal / has_subject decision table.
    """
    referring_signal, matched = _detect_referring_signal(question)
    has_subject = _has_subject(question)

    if referring_signal:
        classification: FollowupClassification = "followup" if has_history else "ambiguous"
    elif has_subject:
        classification = "standalone"
    else:
        classification = "ambiguous"

    return ClassificationResult(
        classification=classification,
        referring_signal=referring_signal,
        has_subject=has_subject,
        matched_patterns=matched,
    )
