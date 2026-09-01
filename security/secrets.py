"""A string subclass that redacts itself in repr()/debugging output.

Protects against a class of accidental-secret-exposure bug that a call-site
fix alone can't fully close: an errant `logger.debug("state=%r", settings)`,
a debugger inspecting a `Settings` object, or a traceback's default
local-variable dump (many exception-reporting tools capture locals via
`repr()`) would otherwise print a real password in cleartext.

This is one additional layer, not a guarantee. A `SecretStr` is still a real
`str` for every purpose that needs the actual value -- equality checks,
passing it to `sqlalchemy.engine.URL.create(password=...)`, or `str()`/
`__format__` (used by plain f-string interpolation) -- only `repr()`/`%r`
are redacted. Deliberate misuse (`logger.info(f"password={settings.db_password}")`,
which uses `__format__`/`__str__`, not `__repr__`) is not, and cannot be,
prevented by a type alone; `security.redaction` is the separate, independent
layer for text that's already been turned into a plain string (e.g. a
caught exception's message) rather than read directly off `Settings`.
"""

from __future__ import annotations

_REDACTED_REPR = "SecretStr('***REDACTED***')"


class SecretStr(str):
    """A `str` whose `repr()`/`%r` output never reveals the real value.

    Equality, hashing, `str()`, and use as a plain string argument (e.g.
    `sqlalchemy.engine.URL.create(password=secret_str_instance)`) all behave
    exactly like the wrapped string -- `str` subclassing is transparent for
    those. Only `repr()` differs, which is what `%r`, `logger.debug("%r", x)`,
    a debugger's variable inspector, and most exception-reporting tools'
    local-variable dumps all use to render a value.
    """

    def __repr__(self) -> str:
        return _REDACTED_REPR


def as_secret(value: str | None) -> SecretStr | None:
    """Wraps `value` in a `SecretStr`, or returns None unchanged.

    Idempotent -- wrapping an already-`SecretStr` value returns an
    equivalent `SecretStr` rather than double-wrapping or raising.
    """
    if value is None:
        return None
    return SecretStr(value)
