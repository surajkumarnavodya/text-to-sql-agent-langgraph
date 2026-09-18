"""Password-strength validation -- the actual policy behind
`Settings.password_min_length`, now covering more than just length.

Deliberately **not** a separate config-driven "provider" the way e.g.
`search/web_search.py`'s `SUPPORTED_SEARCH_PROVIDERS` is -- this project's
authentication provider *is* itself (`identity/`, Argon2id hashing, locally
issued JWTs -- see `identity/security.py`'s own docstring), so "use the
actual authentication provider's capabilities" means implementing the
policy here directly, not deferring to an external IdP's own password
rules the way this app would if it only ever validated externally-issued
OIDC tokens.

Every rule below is checked server-side in `api/identity_auth.py` (the
authoritative enforcement point) and mirrored client-side in
`frontend/src/lib/passwordPolicy.ts` for UX only -- a client-side bypass
changes nothing, since `validate_password_strength` is the only function
that actually gates account creation/password changes.
"""

from __future__ import annotations

import re

from config.settings import Settings

# A small, hand-picked list of passwords that are drastically
# overrepresented in real-world credential-stuffing/breach corpora --
# **not** a full breached-password database (that would require a live
# network call to a third-party service such as HaveIBeenPwned's k-anonymity
# API, a real, disclosed exception to this project's local-first posture
# this pass deliberately does not add -- see docs/authentication-and-password-policy.md's
# "Known limitations" section). This list exists to catch the most
# egregious, purely-length-based bypasses (e.g. "password1234567" clears a
# 12-character minimum but is still one of the most-breached strings in
# existence).
_COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "password1",
        "password123",
        "password1234",
        "password12345",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty123",
        "qwertyuiop",
        "letmein123",
        "welcome123",
        "admin12345",
        "iloveyou123",
        "sunshine123",
        "princess123",
        "football123",
        "monkey12345",
        "dragon12345",
        "master12345",
        "abc123456789",
        "trustno1234",
        "superman123",
        "batman12345",
        "michael1234",
        "jennifer123",
        "computer123",
        "internet123",
        "changeme123",
        "passw0rd123",
        "p@ssw0rd123",
        "p@ssword123",
        "letmein12345",
        "administrator",
    }
)

# A single character (or short 2-3 char unit) repeated to fill most of the
# password, e.g. "aaaaaaaaaaaa" or "ababababab" -- a real, common weak
# pattern length alone doesn't catch.
_LOW_VARIETY_RE = re.compile(r"^(.{1,3})\1{3,}$")

# Common keyboard-walk substrings -- checked as a case-insensitive substring
# match against the password (with digits/punctuation stripped first), so
# "Qwerty123!" still matches "qwerty".
_KEYBOARD_WALKS = ("qwerty", "asdfgh", "zxcvbn", "qazwsx", "1qaz2wsx")

# A dictionary word immediately followed by a bare digit run is the single
# most common weak-password shape in real breach corpora (the examples this
# module's own docstring/design spec calls out by name: "Password123",
# "Abcdefgh123", "Aa123456789") -- checked by stripping a trailing digit
# run and comparing the remainder against this small base-word list, which
# catches the whole "<base word><any digits>" family without needing one
# entry per possible digit suffix in `_COMMON_PASSWORDS` above.
_COMMON_BASE_WORDS = frozenset(
    {
        "password",
        "qwerty",
        "admin",
        "welcome",
        "letmein",
        "dragon",
        "master",
        "superman",
        "batman",
        "iloveyou",
        "sunshine",
        "princess",
        "football",
        "monkey",
        "trustno",
        "changeme",
        "passw0rd",
        "abcdefgh",
        "abcdefg",
        "michael",
        "jennifer",
        "computer",
        "internet",
    }
)

_TRAILING_DIGITS_RE = re.compile(r"\d+$")
_DIGIT_RUN_RE = re.compile(r"\d+")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")

# A purely-sequential ascending or descending digit run at least this long
# (e.g. "123456", "987654") is flagged -- unbounded family by construction
# (works for any starting digit/length), not a fixed list of literal strings.
_MIN_SEQUENTIAL_DIGIT_RUN = 6


def _strip_non_alnum_lower(value: str) -> str:
    return _NON_ALNUM_RE.sub("", value.lower())


def _has_common_base_word(stripped: str) -> bool:
    """True if `stripped` (already alnum-only, lowercased) *is* a common
    weak password, or becomes one once a trailing digit run is removed
    (`"password123456"` -> `"password"`)."""
    if stripped in _COMMON_BASE_WORDS:
        return True
    without_trailing_digits = _TRAILING_DIGITS_RE.sub("", stripped)
    return without_trailing_digits in _COMMON_BASE_WORDS


def _has_sequential_digit_run(stripped: str, min_run: int = _MIN_SEQUENTIAL_DIGIT_RUN) -> bool:
    """True if `stripped` contains an ascending or descending run of
    consecutive digits at least `min_run` long anywhere in it (e.g.
    "abc123456xyz" -> True for the embedded "123456")."""
    for run in _DIGIT_RUN_RE.findall(stripped):
        if len(run) < min_run:
            continue
        deltas = [int(run[i + 1]) - int(run[i]) for i in range(len(run) - 1)]
        if all(d == 1 for d in deltas) or all(d == -1 for d in deltas):
            return True
    return False


def _contains_identity_fragment(
    password_lower: str, fragment: str | None, min_len: int = 4
) -> bool:
    """True if `fragment` (an email local-part, display name, or username)
    is a substantial substring of the password -- `min_len` guards against
    a coincidental short match (e.g. a 2-letter username matching two
    letters buried in an otherwise-strong passphrase) producing a false
    positive."""
    if not fragment:
        return False
    normalized = _strip_non_alnum_lower(fragment)
    if len(normalized) < min_len:
        return False
    return normalized in _strip_non_alnum_lower(password_lower)


def validate_password_strength(
    password: str,
    *,
    settings: Settings,
    email: str | None = None,
    display_name: str | None = None,
    username: str | None = None,
) -> list[str]:
    """Returns a list of human-readable violation messages -- empty means
    the password is accepted.

    Deliberately returns *every* violation found (not just the first) so a
    caller can show the user a complete list in one round-trip, rather than
    a frustrating "fix one thing, submit, get told about the next thing"
    loop.

    Rules (see this module's own docstring + `docs/authentication-and-password-policy.md`
    for the full rationale of each):
        - Length: `Settings.password_min_length` (default 12) to
          `Settings.password_max_length` (default 64) -- inclusive on both
          ends. Never truncated; a too-long password is rejected outright,
          never silently cut.
        - Spaces are explicitly allowed (passphrases) -- never rejected.
        - Rejects an exact (case-insensitive) match against a small
          known-weak-password list, a purely sequential digit run, a
          low-character-variety repeated pattern, or a common keyboard-walk
          substring.
        - Rejects a password containing the caller's own email local-part,
          display name, or username as a substantial substring.
    """
    violations: list[str] = []
    length = len(password)

    if length < settings.password_min_length:
        violations.append(f"Password must be at least {settings.password_min_length} characters.")
    if length > settings.password_max_length:
        violations.append(f"Password must be at most {settings.password_max_length} characters.")

    lowered = password.lower()
    stripped = _strip_non_alnum_lower(password)

    if (
        lowered in _COMMON_PASSWORDS
        or stripped in _COMMON_PASSWORDS
        or _has_common_base_word(stripped)
    ):
        violations.append("This password is too common. Please choose a less predictable one.")
    if _has_sequential_digit_run(stripped):
        violations.append("Password must not contain a long sequential digit run (e.g. 123456).")
    if _LOW_VARIETY_RE.match(stripped) and len(stripped) >= 8:
        violations.append("Password must not be a short pattern repeated over and over.")
    if any(walk in stripped for walk in _KEYBOARD_WALKS):
        violations.append("Password must not contain a common keyboard pattern (e.g. qwerty).")

    email_local_part = email.split("@", 1)[0] if email and "@" in email else email
    if _contains_identity_fragment(lowered, email_local_part):
        violations.append("Password must not contain your email address.")
    if _contains_identity_fragment(lowered, display_name):
        violations.append("Password must not contain your display name.")
    if _contains_identity_fragment(lowered, username):
        violations.append("Password must not contain your username.")

    return violations
