"""Unit tests for security/secrets.py's SecretStr (a thin re-export of
pydantic.SecretStr -- see that module's docstring for why)."""

from __future__ import annotations

import pytest

from security.secrets import SecretStr


class TestSecretStr:
    def test_repr_never_reveals_the_value(self):
        secret = SecretStr("hunter2")
        assert "hunter2" not in repr(secret)

    def test_percent_r_formatting_is_redacted(self):
        """logging's `%r`-style formatting (what `logger.debug("%r", x)`
        uses internally) goes through the same `__repr__`."""
        secret = SecretStr("hunter2")
        formatted = "%r" % (secret,)  # noqa: UP031 - intentionally testing %-style formatting
        assert "hunter2" not in formatted

    def test_str_and_format_also_mask_the_value(self):
        """Unlike the project's old hand-rolled SecretStr, pydantic's
        version masks str()/f-string interpolation too -- only
        .get_secret_value() returns the real value. Every call site that
        needs the actual secret (SQLAlchemy's URL builder, a bearer-token
        comparison, a redaction search-string, a third-party API payload)
        must call it explicitly -- see security/secrets.py's docstring for
        the full list this migration had to fix."""
        secret = SecretStr("hunter2")
        assert str(secret) != "hunter2"
        assert f"{secret}" != "hunter2"
        assert secret.get_secret_value() == "hunter2"

    def test_equality_compares_the_wrapped_value_not_the_raw_string(self):
        assert SecretStr("hunter2") == SecretStr("hunter2")
        assert SecretStr("hunter2") != SecretStr("other")
        # Deliberately NOT transparently equal to a raw string -- a real
        # semantic difference from the old str-subclass version, not an
        # oversight (see security/secrets.py's module docstring).
        assert SecretStr("hunter2") != "hunter2"

    def test_truthiness_matches_the_wrapped_string(self):
        assert bool(SecretStr("hunter2")) is True
        assert bool(SecretStr("")) is False

    def test_len_matches_the_wrapped_string(self):
        assert len(SecretStr("hunter2")) == 7

    def test_no_longer_a_str_subclass(self):
        """Documents the deliberate behavior change this migration made:
        str methods like .upper() are gone -- only .get_secret_value() (and
        transformations applied after calling it) reach the real value."""
        assert not isinstance(SecretStr("hunter2"), str)
        with pytest.raises(AttributeError):
            SecretStr("hunter2").upper()  # type: ignore[attr-defined]
