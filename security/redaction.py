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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.settings import Settings

_REDACTED = "***REDACTED***"

# Generic fallback for connection-string-shaped secrets: `password=`/`pwd=`
# followed by a run of non-whitespace/non-`;`/non-`&` characters (covers
# both `key=value;key=value` DSN style and `key=value&key=value` URL-query
# style), and the `://user:password@host` URL-credentials shape (username
# preserved in the redacted output -- only the password itself is
# sensitive). Applied regardless of whether the exact configured password
# was matched first -- a driver can render the same secret differently than
# `Settings` stores it (URL-encoded, re-cased, ...).
_CONNECTION_STRING_SECRET_RE = re.compile(
    r"(?P<key>password|pwd)\s*=\s*(?P<value>[^;&\s]+)"
    r"|"
    r"://(?P<user>[^\s:/@]+):(?P<pass>[^\s@]+)@",
    re.IGNORECASE,
)


def _redact_match(match: re.Match[str]) -> str:
    if match.group("key") is not None:
        return f"{match.group('key')}={_REDACTED}"
    return f"://{match.group('user')}:{_REDACTED}@"


def redact_secrets(text: str, settings: Settings | None = None) -> str:
    """Returns `text` with any known secret value replaced by a placeholder.

    Safe to call on text with no secret in it at all -- returns it
    unchanged in that case. Intended for exactly one kind of input: text
    this app did not construct itself (a caught exception's `str(exc)`, a
    driver's own error message) before it is ever logged or shown to a
    user.

    Args:
        text: The text to redact (e.g. `str(exc)` from a connection or
            query-execution failure).
        settings: Settings to pull the configured secret value(s) from.
            None skips the exact-value layer and applies only the generic
            regex fallback (e.g. for a caller with no `Settings` in scope).

    Returns:
        Redacted text. Never raises -- a redaction bug must not be the
        reason a legitimate error message can't be shown; the generic
        regex layer still applies even if the exact-value layer finds
        nothing to replace.
    """
    redacted = text
    if settings is not None and settings.db_password:
        password = str(settings.db_password)
        if password:
            redacted = redacted.replace(password, _REDACTED)
    return _CONNECTION_STRING_SECRET_RE.sub(_redact_match, redacted)
