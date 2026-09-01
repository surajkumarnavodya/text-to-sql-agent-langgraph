"""Unit tests for security/secrets.py's SecretStr."""

from __future__ import annotations

from security.secrets import SecretStr, as_secret


class TestSecretStr:
    def test_repr_never_reveals_the_value(self):
        secret = SecretStr("hunter2")
        assert "hunter2" not in repr(secret)
        assert repr(secret) == "SecretStr('***REDACTED***')"

    def test_percent_r_formatting_is_redacted(self):
        """logging's `%r`-style formatting (what `logger.debug("%r", x)`
        uses internally) goes through the same `__repr__`."""
        secret = SecretStr("hunter2")
        formatted = "%r" % (secret,)  # noqa: UP031 - intentionally testing %-style formatting
        assert "hunter2" not in formatted

    def test_str_and_format_still_reveal_the_real_value(self):
        """Documents the known, deliberate limitation: only repr() is
        redacted -- str()/f-string interpolation still yields the real
        value, since callers that need the actual secret (e.g.
        SQLAlchemy's URL builder) must still get it."""
        secret = SecretStr("hunter2")
        assert str(secret) == "hunter2"
        assert f"{secret}" == "hunter2"

    def test_equality_and_use_as_a_plain_string_is_transparent(self):
        secret = SecretStr("hunter2")
        assert secret == "hunter2"
        assert secret.upper() == "HUNTER2"
        assert len(secret) == 7

    def test_truthiness_matches_the_wrapped_string(self):
        assert bool(SecretStr("hunter2")) is True
        assert bool(SecretStr("")) is False


class TestAsSecret:
    def test_wraps_a_plain_string(self):
        wrapped = as_secret("hunter2")
        assert isinstance(wrapped, SecretStr)
        assert wrapped == "hunter2"

    def test_none_passes_through_unchanged(self):
        assert as_secret(None) is None

    def test_idempotent_on_an_already_wrapped_value(self):
        once = as_secret("hunter2")
        twice = as_secret(once)
        assert isinstance(twice, SecretStr)
        assert twice == "hunter2"
