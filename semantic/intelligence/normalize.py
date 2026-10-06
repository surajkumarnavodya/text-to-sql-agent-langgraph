"""Deterministic term normalization for the semantic-intelligence engine (Prompt 35).

Two business terms are compared through a canonical form: lowercase, camelCase
and underscores split, a small curated abbreviation map applied, simple plural
folding, stopwords dropped, and the remaining tokens sorted. "SalesAmount",
"sales_amount" and "Amount of sales" all reduce to the same canonical string.

This is deliberately not fuzzy matching. Edit-distance ratios match "date" to
"update" and "rate" to "date", which is exactly the false-match class the
prompt asks us to avoid. Equality on a canonical form, plus token overlap for
multi-word terms, is what the detectors use.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Curated, illustrative. A deployment extends this from its own reviewed glossary.
ABBREVIATIONS: dict[str, str] = {
    "qty": "quantity",
    "rev": "revenue",
    "cust": "customer",
    "amt": "amount",
    "num": "number",
    "avg": "average",
}

# Words that carry no business meaning on their own.
_STOPWORDS = frozenset({"the", "a", "an", "of", "by", "per", "for", "in", "on", "to", "and"})

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_TOKEN = re.compile(r"[a-z0-9]+")


def _fold_plural(token: str) -> str:
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


@lru_cache(maxsize=65536)
def content_tokens(text: str) -> frozenset[str]:
    """The business-meaningful tokens of `text`, after folding. Empty for an
    input with no letters or digits."""
    split_camel = _CAMEL.sub(r"\1 \2", text)
    tokens = _TOKEN.findall(split_camel.lower())
    folded = {_fold_plural(ABBREVIATIONS.get(t, t)) for t in tokens}
    return frozenset(t for t in folded if t and t not in _STOPWORDS)


@lru_cache(maxsize=65536)
def canonical(text: str) -> str:
    """Order-insensitive canonical form. Empty string when there are no content
    tokens, so callers can skip terms that normalize to nothing."""
    return " ".join(sorted(content_tokens(text)))


def token_overlap(a: str, b: str) -> float:
    """Jaccard similarity over content tokens. 0.0 when either side is empty."""
    ta, tb = content_tokens(a), content_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Minimum Jaccard for two multi-word terms to be treated as the same concept.
# 0.75 is deliberately strict: "net sales" vs "net sales amount" (0.67) stays
# separate, so a term is never merged on a single shared word.
OVERLAP_THRESHOLD = 0.75


def same_concept(a: str, b: str) -> tuple[bool, str, float]:
    """Whether two terms name the same concept.

    Returns `(matched, method, confidence)`. Equal canonical forms are a
    "canonical_match" at 0.9. Multi-word terms that meet `OVERLAP_THRESHOLD`
    are a "token_overlap" at the overlap itself. A single-token term matches
    only on canonical equality, never on overlap.
    """
    ca, cb = canonical(a), canonical(b)
    if not ca or not cb:
        return False, "", 0.0
    if ca == cb:
        return True, "canonical_match", 0.9
    if len(content_tokens(a)) >= 2 and len(content_tokens(b)) >= 2:
        overlap = token_overlap(a, b)
        if overlap >= OVERLAP_THRESHOLD:
            return True, "token_overlap", round(overlap * 0.8, 3)
    return False, "", 0.0
