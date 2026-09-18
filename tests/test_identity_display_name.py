"""Unit tests for identity/display_name.py, plus HTTP-level coverage that
`RegisterRequest`/`UpdateProfileRequest` actually enforce it (mandatory at
sign-up -- this feature's own explicit requirement)."""

from __future__ import annotations

import pytest
from identity.display_name import validate_display_name
from pydantic import ValidationError


class TestValidateDisplayName:
    def test_trims_leading_and_trailing_whitespace(self):
        assert validate_display_name("  Alice  ") == "Alice"

    def test_collapses_internal_whitespace_runs(self):
        assert validate_display_name("Alice    Smith") == "Alice Smith"

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            validate_display_name("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValueError):
            validate_display_name("     ")

    def test_rejects_too_short(self):
        with pytest.raises(ValueError):
            validate_display_name("A")

    def test_accepts_minimum_length(self):
        assert validate_display_name("Al") == "Al"

    def test_rejects_too_long(self):
        with pytest.raises(ValueError):
            validate_display_name("A" * 101)

    def test_accepts_maximum_length(self):
        assert validate_display_name("A" * 100) == "A" * 100

    def test_rejects_control_characters(self):
        with pytest.raises(ValueError):
            validate_display_name("Alice\x00Smith")

    def test_rejects_newline(self):
        with pytest.raises(ValueError):
            validate_display_name("Alice\nSmith")

    def test_rejects_html_angle_brackets(self):
        with pytest.raises(ValueError):
            validate_display_name("<script>alert(1)</script>")

    def test_unicode_name_is_normalized_and_accepted(self):
        # NFKC-normalizes full-width Latin letters to their standard form.
        assert validate_display_name("Ｍａｒｉａ") == "Maria"

    def test_accented_unicode_name_is_accepted(self):
        assert validate_display_name("José García") == "José García"


from identity.schemas import RegisterRequest, UpdateProfileRequest  # noqa: E402


class TestRegisterRequestRequiresDisplayName:
    def test_missing_display_name_is_rejected(self):
        with pytest.raises(ValidationError):
            RegisterRequest(email="a@example.com", password="correct horse battery staple 1")

    def test_empty_display_name_is_rejected(self):
        with pytest.raises(ValidationError):
            RegisterRequest(
                email="a@example.com",
                password="correct horse battery staple 1",
                display_name="",
            )

    def test_whitespace_only_display_name_is_rejected(self):
        with pytest.raises(ValidationError):
            RegisterRequest(
                email="a@example.com",
                password="correct horse battery staple 1",
                display_name="   ",
            )

    def test_valid_display_name_is_accepted_and_trimmed(self):
        request = RegisterRequest(
            email="a@example.com",
            password="correct horse battery staple 1",
            display_name="  Alice  ",
        )
        assert request.display_name == "Alice"


class TestUpdateProfileRequest:
    def test_valid_display_name_accepted(self):
        request = UpdateProfileRequest(display_name="Bob")
        assert request.display_name == "Bob"

    def test_invalid_display_name_rejected(self):
        with pytest.raises(ValidationError):
            UpdateProfileRequest(display_name="<b>Bob</b>")
