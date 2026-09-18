"""Display-name validation -- shared by `RegisterRequest` (mandatory at
sign-up) and `UpdateProfileRequest` (the profile-completion path for an
account created before this field was required -- see
`api/identity_auth.py::update_profile`).

Deliberately its own small module rather than inline in `identity/schemas.py`
(a Pydantic `field_validator` needs the actual validation logic somewhere
callable) and never used as an identity key anywhere in this codebase --
`users.email`/`users.id` remain the only identifiers a lookup or a foreign
key is ever built from; `display_name` is purely a rendered label.
"""

from __future__ import annotations

import re
import unicodedata

MIN_LENGTH = 2
MAX_LENGTH = 100

# C0/C1 control characters + DEL -- a pasted display name should never be
# able to inject a newline, a null byte, or a terminal-escape sequence into
# logs/UI rendering.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# A blunt but effective guard against HTML/script injection via a display
# name: `<`/`>` have no legitimate use in a person's name, and rejecting
# them outright is simpler and more robust than trying to allow-list a safe
# subset of markup (this app also never renders a display name as raw HTML
# -- React's default JSX text interpolation already escapes it -- so this
# is defense-in-depth, not the only thing standing between an attacker and
# a stored-XSS bug).
_HTML_ISH_RE = re.compile(r"[<>]")


def validate_display_name(value: str) -> str:
    """Normalizes and validates a display name, returning the cleaned value.

    Raises:
        ValueError: on any violation -- caught by Pydantic's `field_validator`
            machinery and turned into a normal 422 response with a clear
            per-field message, matching every other validated field in this
            codebase's request models.
    """
    # NFKC normalization folds visually-confusable/compatibility Unicode
    # variants (e.g. full-width Latin letters, certain combining forms)
    # into their standard form *before* length/character checks run --
    # the same normalization `security.sanitization.normalize_text` already
    # applies to schema-adjacent text elsewhere in this codebase, for the
    # same "don't let a lookalike character sneak past a naive check"
    # reasoning.
    normalized = unicodedata.normalize("NFKC", value).strip()

    if not normalized:
        raise ValueError("Display name is required.")
    # Checked *before* collapsing whitespace below -- a newline/tab is both
    # a control character and whitespace, and this must reject it as the
    # former rather than silently flatten it into an ordinary space first.
    if _CONTROL_CHAR_RE.search(normalized):
        raise ValueError("Display name must not contain control characters.")
    if _HTML_ISH_RE.search(normalized):
        raise ValueError("Display name must not contain '<' or '>'.")

    normalized = re.sub(r"[ \t]+", " ", normalized)

    if len(normalized) < MIN_LENGTH:
        raise ValueError(f"Display name must be at least {MIN_LENGTH} characters.")
    if len(normalized) > MAX_LENGTH:
        raise ValueError(f"Display name must be at most {MAX_LENGTH} characters.")
    return normalized
