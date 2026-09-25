"""Redacts known secret values out of text before it's logged or displayed.

The database driver this app talks to is not something this project
controls the error-message format of -- a connection failure can surface as
anything from a clean "connection refused" to, on some drivers/failure
modes, the full attempted connection string (including the password)
embedded verbatim in the exception text. `db/connection.py`'s own docstring
already promises "never log the password or the full connection string" as
an app-level discipline for text this codebase writes itself; this module is
what makes that promise hold even for text this codebase did *not* write
(`str(exc)` from a third-party driver).

Two independent layers, since either alone can miss a shape the other
catches:
  1. **Exact-value redaction** of the specific secret this process is
     actually configured with (`Settings.db_password`) -- catches the
     common case directly and verbatim, regardless of surrounding text
     shape.
  2. **A generic regex fallback** for connection-string-shaped
     `password=...`/`pwd=...` and `://user:password@host` patterns --
     catches it even if the exact-value match misses (e.g. the driver
     rendered a URL-encoded or differently-cased variant of the same
     password).

Layered, not a guarantee: an unanticipated way a driver might render a
secret (a format neither layer matches) is a real, standing residual risk,
stated plainly rather than hidden -- the same honesty this project already
applies to its other denylist-shaped defenses (see
`agent/input_guard.py`, `agent/sql_validator.py`'s dangerous-function
check). This module exists specifically for text this app did not
generate itself; text this codebase writes itself should simply never
interpolate a secret into a log/display string in the first place --
that's the existing, primary discipline documented in
`config/settings.py` and `db/connection.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config.settings import Settings
    from db.connection import DbConnectionLike

_REDACTED = "***REDACTED***"

# Generic fallback for connection-string-/API-key-shaped secrets:
# `password=`/`pwd=` followed by a run of non-whitespace/non-`;`/non-`&`
# characters (covers both `key=value;key=value` DSN style and
# `key=value&key=value` URL-query style), the `://user:password@host`
# URL-credentials shape (username preserved in the redacted output -- only
# the password itself is sensitive), an AWS-style access key ID, and a
# bearer token. Applied regardless of whether the exact configured value
# was matched first -- a driver (or an LLM paraphrasing/re-rendering
# something it saw) can render the same secret differently than `Settings`
# stores it (URL-encoded, re-cased, ...). The AKIA/Bearer alternatives
# exist primarily for `redact_configured_secrets`'s LLM-response-text use
# case (see that function) -- a driver error is unlikely to contain either
# shape, but including them here rather than a second, parallel regex
# costs nothing and keeps one canonical pattern.
_CONNECTION_STRING_SECRET_RE = re.compile(
    r"(?P<key>password|pwd)\s*=\s*(?P<value>[^;&\s]+)"
    r"|"
    r"://(?P<user>[^\s:/@]+):(?P<pass>[^\s@]+)@"
    r"|"
    r"\bAKIA[0-9A-Z]{16}\b"
    r"|"
    r"\bBearer\s+[A-Za-z0-9\-_.]{20,}\b",
    re.IGNORECASE,
)


def _redact_match(match: re.Match[str]) -> str:
    if match.group("key") is not None:
        return f"{match.group('key')}={_REDACTED}"
    if match.group("user") is not None:
        return f"://{match.group('user')}:{_REDACTED}@"
    return _REDACTED


def redact_secrets(text: str, settings: DbConnectionLike | None = None) -> str:
    """Returns `text` with any known secret value replaced by a placeholder.

    Safe to call on text with no secret in it at all -- returns it
    unchanged in that case. Intended for exactly one kind of input: text
    this app did not construct itself (a caught exception's `str(exc)`, a
    driver's own error message) before it is ever logged or shown to a
    user.

    Args:
        text: The text to redact (e.g. `str(exc)` from a connection or
            query-execution failure).
        settings: The `Settings` (or one specific `Settings.databases`
            entry -- both have a `db_password`) to pull the configured
            secret value from. Passing the *specific* connection actually
            being tested/queried matters once multiple databases are
            configured: their passwords can differ, and only the exact one
            in play is redacted by this exact-value layer (the generic
            regex fallback below still catches most other passwords'
            shape regardless). None skips the exact-value layer and
            applies only the generic regex fallback.

    Returns:
        Redacted text. Never raises -- a redaction bug must not be the
        reason a legitimate error message can't be shown; the generic
        regex layer still applies even if the exact-value layer finds
        nothing to replace.
    """
    redacted = text
    if settings is not None and settings.db_password:
        password = settings.db_password.get_secret_value()
        # isinstance, not just truthiness: a test double standing in for
        # `settings` (a bare `unittest.mock.MagicMock()`, common across this
        # codebase's test suite) makes `.db_password.get_secret_value()`
        # itself a truthy `MagicMock`, not a string -- `str.replace` would
        # raise on that, which is exactly the "redaction bug" this
        # function's own docstring promises never to become the reason a
        # legitimate error message can't be shown.
        if isinstance(password, str) and password:
            redacted = redacted.replace(password, _REDACTED)
    return _CONNECTION_STRING_SECRET_RE.sub(_redact_match, redacted)


@dataclass(frozen=True)
class SecretFingerprint:
    """One configured secret's label + real value, for exact-match redaction.

    The label is what's safe to log/report; the value itself never is --
    see `redact_configured_secrets` and (for the read-only reporting use of
    the same fingerprints) `eval.security_benchmark.detectors
    .detect_secret_leak`, which imports this type directly rather than
    keeping its own copy.
    """

    label: str
    value: str


def configured_secret_fingerprints(settings: Settings) -> tuple[SecretFingerprint, ...]:
    """Every real secret *value* this process is actually configured with,
    labeled for redaction/reporting but never paired with the plaintext in
    anything meant to be logged or displayed.

    Deliberately broader than `redact_secrets`'s own `settings.db_password`
    (that function's original, narrower purpose is redacting a raw DB
    driver error, where only the one connection actually being tested is
    relevant) -- this covers every secret field `config/settings.py`
    defines, across every configured database, since any of them ending up
    in LLM-generated response text would be an equally real incident
    regardless of which one it is.
    """
    fingerprints: list[SecretFingerprint] = []

    def _add(label: str, secret: Any) -> None:
        if secret is None:
            return
        value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
        if isinstance(value, str) and len(value) >= 4:
            fingerprints.append(SecretFingerprint(label, value))

    _add("db_password", settings.db_password)
    _add("db_connection_string", settings.db_connection_string)
    for db in settings.databases:
        _add(f"db_password[{db.name}]", db.db_password)
        _add(f"db_connection_string[{db.name}]", db.db_connection_string)
    _add("api_auth_token", settings.api_auth_token)
    _add("auth_database_url", settings.auth_database_url)
    _add("jwt_secret_key", settings.jwt_secret_key)
    _add("bootstrap_admin_password", settings.bootstrap_admin_password)
    _add("rag_store_connection_string", settings.rag_store_connection_string)
    _add("web_search_api_key", settings.web_search_api_key)
    _add("ima_api_key", settings.ima_api_key)
    _add("azure_content_safety_key", settings.azure_content_safety_key)
    _add("moderation_store_connection_string", settings.moderation_store_connection_string)
    return tuple(fingerprints)


def redact_configured_secrets(text: str, settings: Settings) -> str:
    """Returns `text` with every secret value `settings` is actually
    configured with replaced by a placeholder, plus the same generic
    connection-string/API-key regex fallback `redact_secrets` applies.

    Unlike `redact_secrets` (aimed narrowly at a raw DB driver error
    string), this is aimed at **LLM-generated response text** -- `insight`,
    `synthesized_answer`, `sql`, `query_plan`, error/rejection/clarification
    messages, and every orchestrator source's own answer text. None of
    these should ever legitimately contain a real secret (no secret value
    is intentionally fed into any prompt), but a model can only be trusted
    not to repeat back something it was never shown in the first place up
    to the limits of every *other* control in this codebase actually
    holding -- this is a last-line, defense-in-depth net for the case one
    of them doesn't, not a substitute for keeping secrets out of context to
    begin with. Safe to call unconditionally: returns `text` unchanged if
    nothing configured matches.
    """
    redacted = text
    for fingerprint in configured_secret_fingerprints(settings):
        if fingerprint.value in redacted:
            redacted = redacted.replace(fingerprint.value, _REDACTED)
    return _CONNECTION_STRING_SECRET_RE.sub(_redact_match, redacted)
