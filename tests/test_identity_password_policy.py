"""Unit tests for identity/password_policy.py."""

from __future__ import annotations

from identity.password_policy import validate_password_strength

from config.settings import Settings


def _settings(**overrides) -> Settings:
    base = {"password_min_length": 12, "password_max_length": 64}
    base.update(overrides)
    return Settings(**base)


class TestLength:
    def test_missing_password_is_rejected(self):
        violations = validate_password_strength("", settings=_settings())
        assert violations

    def test_shorter_than_minimum_is_rejected(self):
        violations = validate_password_strength("Short1!", settings=_settings())
        assert any("at least 12" in v for v in violations)

    def test_at_minimum_length_with_no_other_issues_passes(self):
        violations = validate_password_strength("correct horse battery", settings=_settings())
        assert violations == []

    def test_maximum_length_password_is_accepted(self):
        # A long, high-entropy passphrase at exactly the configured max.
        password = ("Tr0ub4dor&Zebra Giraffe Mountain River Forest " * 2)[:64]
        violations = validate_password_strength(password, settings=_settings())
        assert violations == []

    def test_longer_than_maximum_is_rejected_not_truncated(self):
        password = "a" * 5 + " correct horse battery staple mountain river " * 3
        violations = validate_password_strength(password, settings=_settings())
        assert any("at most 64" in v for v in violations)


class TestPassphrasesAndSpaces:
    def test_strong_passphrase_with_spaces_is_accepted(self):
        violations = validate_password_strength(
            "purple elephant drinks coffee daily", settings=_settings()
        )
        assert violations == []


class TestCommonAndPatternedPasswords:
    def test_common_password_is_rejected(self):
        violations = validate_password_strength("password123456", settings=_settings())
        assert any("too common" in v for v in violations)

    def test_sequential_digits_are_rejected(self):
        violations = validate_password_strength("abcXYZ123456789", settings=_settings())
        assert any("sequential digit" in v for v in violations)

    def test_repeated_pattern_is_rejected(self):
        violations = validate_password_strength("abcabcabcabcabc", settings=_settings())
        assert any("repeated" in v for v in violations)

    def test_keyboard_walk_is_rejected(self):
        violations = validate_password_strength("Qwerty123456789", settings=_settings())
        assert any("keyboard" in v for v in violations)

    def test_password123_style_pattern_is_rejected(self):
        violations = validate_password_strength("Password123456", settings=_settings())
        assert any("too common" in v or "sequential" in v for v in violations)


class TestContainsIdentityFragments:
    def test_password_containing_email_is_rejected(self):
        violations = validate_password_strength(
            "johnsmith-secure-99", settings=_settings(), email="johnsmith@example.com"
        )
        assert any("email" in v for v in violations)

    def test_password_containing_display_name_is_rejected(self):
        violations = validate_password_strength(
            "AlexanderTheGreat99!", settings=_settings(), display_name="Alexander"
        )
        assert any("display name" in v for v in violations)

    def test_password_containing_username_is_rejected(self):
        violations = validate_password_strength(
            "jdoe12345secure!", settings=_settings(), username="jdoe1234"
        )
        assert any("username" in v for v in violations)

    def test_short_incidental_overlap_is_not_flagged(self):
        # A 2-3 char coincidental overlap with a short username must not
        # false-positive on an otherwise-strong, unrelated passphrase.
        violations = validate_password_strength(
            "purple elephant drinks coffee", settings=_settings(), username="al"
        )
        assert not any("username" in v for v in violations)

    def test_unrelated_email_does_not_trigger_false_positive(self):
        violations = validate_password_strength(
            "purple elephant drinks coffee", settings=_settings(), email="someoneelse@example.com"
        )
        assert violations == []


class TestUnicodePasswords:
    def test_unicode_passphrase_is_accepted(self):
        violations = validate_password_strength(
            "café münchen пароль 日本語のパスワード", settings=_settings()
        )
        assert violations == []
