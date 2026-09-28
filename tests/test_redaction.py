"""Unit tests for security/redaction.py's redact_secrets.

Regression coverage for a confirmed audit finding: `db/connection.py`'s
`test_connection()` logs and displays raw driver exception text, which can
embed the connection string (including the password) in cleartext on some
drivers/failure modes.
"""

from __future__ import annotations

import json
from pathlib import Path

from config.settings import Settings
from security.redaction import (
    configured_secret_fingerprints,
    redact_configured_secrets,
    redact_secrets,
)
from security.secrets import SecretStr

# A real, well-formed JWT (three base64url segments) -- deterministically
# generated with a throwaway, hardcoded, test-only signing key
# ("test-signing-key-not-real", never used anywhere outside this file) via
# `pyjwt`, not a value that was ever a genuine credential anywhere. Used to
# prove the redaction regex matches the *shape* of a real JWT (Google ID
# token, this app's own locally-issued access token, or any other), not a
# hand-typed approximation of one.
_SAMPLE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiIxMTA0NDMzMjIzMjA5ODc2NTQzMjEiLCJlbWFpbCI6InRlc3RAZXhhbXBsZS5jb20ifQ."
    "kQGJqUPAeRLvy3-9xfoCv-H_IKR8RgyYq-Iabc123XYZ"
)

_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="postgresql",
    db_host="db.example.com",
    db_port=5432,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("S3cr3t!"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="x",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="x",
    embedding_model_name="x",
    schema_top_k=4,
    max_retries=3,
    complex_query_max_retry_bonus=2,
    max_result_rows=1000,
    query_timeout_seconds=15,
    llm_max_tokens=1024,
    insight_max_tokens=120,
    max_question_length=500,
    question_rate_limit_per_minute=10,
    llm_call_rate_limit_per_minute=20,
    cost_estimation_enabled=True,
    cost_estimation_timeout_seconds=3,
    cost_moderate_row_threshold=50_000,
    cost_high_row_threshold=1_000_000,
    log_level="INFO",
    log_redaction_level="standard",
)


def _settings(**overrides: object) -> Settings:
    """A `_BASE_SETTINGS` copy with `overrides` applied -- see
    `tests/test_connection.py::_settings` for why this rebuilds via
    `Settings(**{**_BASE_SETTINGS.__dict__, **overrides})` rather than
    `BaseModel.model_copy(update=...)`."""
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


class TestRedactSecrets:
    def test_exact_configured_password_is_redacted(self):
        text = "driver said: password 'S3cr3t!' was rejected"
        redacted = redact_secrets(text, _settings())
        assert "S3cr3t!" not in redacted
        assert "***REDACTED***" in redacted

    def test_url_embedded_credentials_are_redacted_but_username_kept(self):
        text = (
            "(psycopg2.OperationalError) could not connect to "
            "postgresql://reader:S3cr3t!@db.example.com:5432/mydb"
        )
        redacted = redact_secrets(text, _settings())
        assert "S3cr3t!" not in redacted
        assert "reader" in redacted  # username is not sensitive, kept for diagnosis
        assert "***REDACTED***" in redacted

    def test_dsn_style_password_param_is_redacted_even_without_settings(self):
        """The generic regex fallback works even with settings=None (e.g. a
        caller with no Settings in scope) -- it doesn't depend on the exact
        configured value being known."""
        text = "connection failed: password=hunter2;host=db;dbname=x"
        redacted = redact_secrets(text, None)
        assert "hunter2" not in redacted
        assert "***REDACTED***" in redacted

    def test_pwd_alias_is_also_redacted(self):
        text = "pwd=hunter2&server=db"
        redacted = redact_secrets(text, None)
        assert "hunter2" not in redacted

    def test_text_with_no_secret_is_unchanged(self):
        text = "could not resolve host db.example.com"
        assert redact_secrets(text, _settings()) == text

    def test_none_settings_skips_exact_match_layer_gracefully(self):
        """No exception, no crash -- the exact-value layer just doesn't
        run; the generic regex layer still applies."""
        text = "auth failed"
        assert redact_secrets(text, None) == text

    def test_empty_password_never_matched_against_arbitrary_text(self):
        """A blank/unset password must not become a redaction of every
        occurrence of an empty string (which would corrupt unrelated text)."""
        settings = _settings(db_password=None)
        text = "connection refused"
        assert redact_secrets(text, settings) == text


class TestConfiguredSecretFingerprints:
    def test_db_password_is_fingerprinted(self):
        fingerprints = configured_secret_fingerprints(_settings())
        assert any(fp.label == "db_password" and fp.value == "S3cr3t!" for fp in fingerprints)

    def test_api_auth_token_is_fingerprinted(self):
        settings = _settings(api_auth_token=SecretStr("tok3n-abc123"))
        fingerprints = configured_secret_fingerprints(settings)
        assert any(
            fp.label == "api_auth_token" and fp.value == "tok3n-abc123" for fp in fingerprints
        )

    def test_unset_secret_fields_produce_no_fingerprint(self):
        settings = _settings(db_password=None)
        labels = [fp.label for fp in configured_secret_fingerprints(settings)]
        assert "db_password" not in labels

    def test_short_values_are_not_fingerprinted(self):
        """A very short 'secret' (e.g. a 2-3 char placeholder) is excluded --
        matching it verbatim against arbitrary response text would risk
        redacting unrelated legitimate content that merely contains the same
        short substring by coincidence."""
        settings = _settings(db_password=SecretStr("ab"))
        labels = [fp.label for fp in configured_secret_fingerprints(settings)]
        assert "db_password" not in labels


class TestRedactConfiguredSecrets:
    """Covers the LLM-response-text redaction path -- the defense-in-depth
    net applied at api/main.py's `_ask_response_from_state` boundary (see
    `_redact_text` there), distinct from `redact_secrets`'s original,
    narrower `db_password`-only purpose aimed at raw driver errors."""

    def test_configured_db_password_is_redacted_from_answer_text(self):
        text = "Here is your data. By the way the password is S3cr3t! for debugging."
        redacted = redact_configured_secrets(text, _settings())
        assert "S3cr3t!" not in redacted
        assert "***REDACTED***" in redacted

    def test_api_auth_token_is_redacted_from_answer_text(self):
        settings = _settings(api_auth_token=SecretStr("tok3n-abc123"))
        text = "The configured token is tok3n-abc123, use it for the next call."
        redacted = redact_configured_secrets(text, settings)
        assert "tok3n-abc123" not in redacted

    def test_generic_bearer_token_shape_is_redacted_even_if_not_configured(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdef"
        redacted = redact_configured_secrets(text, _settings())
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdef" not in redacted

    def test_ordinary_answer_text_is_unchanged(self):
        text = "Total sales in 2013 were $1,204,592 across 4 territories."
        assert redact_configured_secrets(text, _settings()) == text


class TestJwtAndOAuthFieldRedaction:
    """Google sign-in (2026-09-28) needs this app's redaction net to catch a
    JWT (a Google ID token, or this app's own locally-issued access token)
    even when it isn't preceded by "Bearer ", plus the OAuth-adjacent field
    names the task's own threat model calls out by name."""

    def test_bare_jwt_with_no_bearer_prefix_is_redacted(self):
        text = f"Google credential received: {_SAMPLE_JWT}"
        redacted = redact_secrets(text, None)
        assert _SAMPLE_JWT not in redacted
        assert "***REDACTED***" in redacted

    def test_bare_jwt_in_a_query_string_is_redacted(self):
        text = f"GET /auth/callback?id_token={_SAMPLE_JWT}&state=xyz HTTP/1.1"
        redacted = redact_secrets(text, None)
        assert _SAMPLE_JWT not in redacted

    def test_client_secret_field_is_redacted(self):
        text = "token exchange failed: client_secret=GOCSPX-fake_secret_value_123"
        redacted = redact_secrets(text, None)
        assert "GOCSPX-fake_secret_value_123" not in redacted
        assert "***REDACTED***" in redacted

    def test_access_token_field_is_redacted(self):
        text = "response body: access_token=ya29.fake-access-token-value&expires_in=3600"
        redacted = redact_secrets(text, None)
        assert "ya29.fake-access-token-value" not in redacted

    def test_refresh_token_field_is_redacted(self):
        text = "Set-Cookie: refresh_token=abcDEF123-opaque-value; Path=/auth"
        redacted = redact_secrets(text, None)
        assert "abcDEF123-opaque-value" not in redacted

    def test_ordinary_prose_with_no_secret_shape_is_unchanged(self):
        text = "The password policy requires at least 12 characters."
        # "password" appears, but with no "=value" following it -- must not
        # be misfired on by the key=value pattern.
        assert redact_secrets(text, None) == text

    def test_ordinary_email_shaped_text_is_not_mistaken_for_a_jwt(self):
        """A real risk with a loose 'three dot-separated segments' pattern:
        an email-adjacent or version-number-shaped string must not
        false-positive. This app's pattern requires an `ey`-prefixed first
        segment specifically (real JWTs always start this way, the base64
        encoding of `{"`), which an email address never does."""
        text = "Contact user.name.test@example.com for details, running v1.2.3 today."
        assert redact_secrets(text, None) == text


class TestNestedStructureMasking:
    """The task's own explicit requirement: masking must hold regardless of
    how deeply a secret is nested inside a larger structure (a logged
    request/response body, a list of header dicts, ...) -- `redact_secrets`/
    `redact_configured_secrets` operate on the final serialized string, so
    nesting depth is irrelevant to whether the *substring* is found, but
    this is worth proving explicitly rather than just assumed."""

    def test_secret_nested_inside_a_json_object_is_redacted(self):
        settings = _settings(api_auth_token=SecretStr("tok3n-abc123"))
        payload = {
            "request": {
                "headers": {"Authorization": "Bearer tok3n-abc123"},
                "body": {"nested": {"deeply": {"api_key": "tok3n-abc123"}}},
            }
        }
        text = json.dumps(payload)
        redacted = redact_configured_secrets(text, settings)
        assert "tok3n-abc123" not in redacted

    def test_secret_nested_inside_a_list_of_dicts_is_redacted(self):
        settings = _settings(api_auth_token=SecretStr("tok3n-abc123"))
        payload = [
            {"event": "request_started"},
            {"event": "auth_header", "value": "Bearer tok3n-abc123"},
            {"event": "request_finished"},
        ]
        text = json.dumps(payload)
        redacted = redact_configured_secrets(text, settings)
        assert "tok3n-abc123" not in redacted

    def test_jwt_nested_inside_a_query_string_inside_a_json_body_is_redacted(self):
        payload = {"request_line": f"GET /callback?id_token={_SAMPLE_JWT} HTTP/1.1"}
        text = json.dumps(payload)
        redacted = redact_secrets(text, None)
        assert _SAMPLE_JWT not in redacted

    def test_multiple_distinct_secrets_in_the_same_structure_are_all_redacted(self):
        settings = _settings(
            api_auth_token=SecretStr("tok3n-abc123"), db_password=SecretStr("S3cr3t!")
        )
        payload = {
            "db_error": "connection failed: password=S3cr3t!;host=db",
            "auth_header": "Bearer tok3n-abc123",
            "credential": _SAMPLE_JWT,
        }
        text = json.dumps(payload)
        redacted = redact_configured_secrets(text, settings)
        assert "S3cr3t!" not in redacted
        assert "tok3n-abc123" not in redacted
        assert _SAMPLE_JWT not in redacted
