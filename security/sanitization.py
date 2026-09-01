"""Text normalization/sanitization primitives -- the shared first line of defense
against both typed user input and stored database content reaching a prompt.

Two distinct tricks are handled, deliberately kept separate because they
require different techniques:

1. **Invisible/control-character obfuscation** -- null bytes, zero-width
   spaces/joiners, C0/C1 control codes. These can split a keyword a naive
   filter is looking for ("ig<ZWSP>nore") without changing how the text
   *looks* when rendered. Handled by `_strip_invisible_and_control` below,
   driven by Unicode general category (`Cc`/`Cf`/`Co`/`Cs`) rather than a
   hand-maintained list of code points.

2. **Homoglyph substitution** -- visually near-identical characters from a
   different script (Cyrillic а for Latin a, Greek ο for Latin o, ...) used
   to sneak a phrase past pattern matching while still reading as English to
   a human. **Unicode NFKC normalization alone does NOT fix this** -- it's a
   common misconception. NFKC folds *compatibility* variants (full-width
   forms, ligatures, superscripts) to their canonical form, but Cyrillic and
   Latin letters are canonically distinct scripts with no compatibility
   decomposition relationship, so NFKC leaves "Ignоre" (Cyrillic о) exactly
   as-is. Real script-mixing defense needs an explicit confusables map (the
   same idea as Unicode's own UTS #39 "confusables" mechanism, scoped here
   to the realistic attack surface for an English-prompted system: the
   Cyrillic/Greek letters that are near-perfect visual matches for common
   Latin ones). This is a character-level visual-equivalence table, not a
   word/keyword blocklist -- it doesn't know or care what any word means.

Both NFKC normalization and the confusables map are applied by
`normalize_text()`, in that order, so both known-normalizable variants
(full-width ASCII, compatibility ligatures) and cross-script lookalikes are
neutralized before any pattern matching happens downstream.
"""

from __future__ import annotations

import re
import unicodedata

# Cyrillic and Greek letters that are near-perfect visual matches for a
# common Latin letter, mapped to that Latin letter. Scoped to the realistic
# attack surface (an English-language system prompt) rather than attempting
# Unicode's full confusables table -- see module docstring.
_CONFUSABLES: dict[str, str] = {
    # Cyrillic lowercase -> Latin
    "а": "a",
    "е": "e",
    "о": "o",
    "р": "p",
    "с": "c",
    "х": "x",
    "у": "y",
    "і": "i",
    "ѕ": "s",
    "ј": "j",
    "һ": "h",
    "к": "k",
    "м": "m",
    "т": "t",
    "в": "b",
    # Cyrillic uppercase -> Latin
    "А": "A",
    "В": "B",
    "Е": "E",
    "К": "K",
    "М": "M",
    "Н": "H",
    "О": "O",
    "Р": "P",
    "С": "C",
    "Т": "T",
    "Х": "X",
    "Ѕ": "S",
    "Ј": "J",
    # Greek lowercase -> Latin
    "α": "a",
    "ο": "o",
    "ρ": "p",
    "υ": "u",
    "κ": "k",
    "ν": "v",
    # Greek uppercase -> Latin
    "Α": "A",
    "Β": "B",
    "Ε": "E",
    "Ζ": "Z",
    "Η": "H",
    "Ι": "I",
    "Κ": "K",
    "Μ": "M",
    "Ν": "N",
    "Ο": "O",
    "Ρ": "P",
    "Τ": "T",
    "Χ": "X",
    "Υ": "Y",
}

# Unicode general categories to strip entirely: Cc = control, Cf = format
# (this is the category zero-width space/joiner/BOM fall under), Co =
# private use, Cs = surrogate. Zs/Zl/Zp (space separators) are handled
# separately below -- collapsed to a single regular space rather than
# dropped, since real questions legitimately contain whitespace.
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs"})

_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _strip_invisible_and_control(text: str) -> str:
    """Removes control/format/private-use/surrogate characters; collapses whitespace.

    Driven by Unicode general category rather than an explicit code-point
    list, so it catches the whole class (null bytes, zero-width spaces and
    joiners, C0/C1 control codes, BOM, ...) uniformly rather than whatever
    specific characters someone thought to enumerate.
    """
    kept_chars = []
    for ch in text:
        # Checked first, deliberately: \t/\n/\r are themselves Unicode
        # category "Cc" (control), so if the _STRIP_CATEGORIES check ran
        # first it would `continue` past them and silently drop them
        # entirely -- collapsing e.g. "Road Bikes\n-- fake comment" into
        # "Road Bikes-- fake comment" with no space, rather than the
        # intended "Road Bikes -- fake comment". Converting to a space
        # first (and only falling through to the category strip for
        # everything else) is what actually prevents a value containing a
        # newline from reading as a new, separate line once rendered.
        if ch in ("\t", "\n", "\r") or unicodedata.category(ch) in ("Zs", "Zl", "Zp"):
            kept_chars.append(" ")
            continue
        if unicodedata.category(ch) in _STRIP_CATEGORIES:
            continue
        kept_chars.append(ch)
    collapsed = _WHITESPACE_RUN_RE.sub(" ", "".join(kept_chars))
    return collapsed.strip()


def normalize_text(text: str) -> str:
    """Normalizes `text` for safe pattern matching and prompt inclusion.

    Pipeline: NFKC normalization (folds compatibility variants -- full-width
    ASCII, ligatures -- to their canonical form) -> confusables substitution
    (folds the common cross-script homoglyphs in `_CONFUSABLES`) -> strip
    invisible/control characters and collapse whitespace.

    This is the single shared entry point both `agent.input_guard` (user
    questions) and `db.schema_introspection` / `db.value_sampling`
    (database-sourced schema/value text) call before any of that text is
    concatenated into an LLM prompt or matched against a pattern.

    Args:
        text: Raw text from any untrusted source (user-typed or
            database-sourced).

    Returns:
        Normalized text, safe to run pattern matching against and safe to
        concatenate into a prompt (still just text -- callers are
        responsible for length limits appropriate to their context; see
        `truncate_for_log` for the logging-specific cap).
    """
    normalized = unicodedata.normalize("NFKC", text)
    folded = "".join(_CONFUSABLES.get(ch, ch) for ch in normalized)
    return _strip_invisible_and_control(folded)


def truncate_for_log(text: str, max_chars: int = 80) -> str:
    """Caps `text` for inclusion in a log line.

    Rejected/flagged input is exactly the kind of text an attacker controls
    and might make arbitrarily long or repetitive specifically to flood logs
    -- a denial-of-service vector in its own right. Every log line that
    includes raw input text anywhere in this codebase should route it
    through this first, never log it unbounded.
    """
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... ({len(text)} chars total)"
