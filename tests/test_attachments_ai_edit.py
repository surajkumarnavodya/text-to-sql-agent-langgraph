"""Unit tests for attachments/ai_edit.py -- the orchestration layer between
the API route and the provider (allowlist, prompt validation, content
policy, rate limiting, idempotency). HTTP-level integration is covered in
tests/test_attachment_api.py::TestAiEdit; this file covers orchestration
details that don't need a full TestClient round trip.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

import agent.rate_limit as rate_limit_module
from attachments.ai_edit import (
    ImageEditNotConfiguredError,
    execute_image_edit,
    get_image_edit_provider,
)
from config.settings import Settings
from media_gen.image_edit_provider import FakeImageEditProvider
from security.secrets import SecretStr

_BASE_SETTINGS = Settings(
    ollama_host="http://localhost:11434",
    ollama_model="llama3.1:8b",
    ollama_request_timeout_seconds=60,
    db_type="postgresql",
    db_host="db.example.com",
    db_port=5432,
    db_name="mydb",
    db_user="reader",
    db_password=SecretStr("secret"),
    db_connection_string=None,
    db_schema=None,
    db_odbc_driver="x",
    chroma_persist_dir=Path("/tmp/chroma"),
    chroma_collection_name="schema_ddl",
    embedding_model_name="all-MiniLM-L6-v2",
    log_level="INFO",
    log_redaction_level="standard",
    media_gen_rate_limit=5,
    media_gen_rate_window_seconds=60.0,
    image_edit_max_prompt_length=50,
)


def _settings(**overrides) -> Settings:
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _reset_media_generation_limiter():
    """`agent.rate_limit`'s media-generation limiter is a process-wide
    singleton `execute_image_edit` shares with plain media generation --
    reset before/after every test so one test's calls can't trip another's
    budget purely by test order (same reasoning `tests/test_orchestrator.py`
    already applies to this exact limiter)."""
    rate_limit_module._media_generation_limiter = None
    yield
    rate_limit_module._media_generation_limiter = None


@pytest.fixture(autouse=True)
def _reset_idempotency_cache():
    """`attachments.ai_edit._idempotency_cache` is a process-wide bounded
    dict -- reset before/after every test so a leftover entry from one
    test's `idempotency_key` can never make a later, unrelated test's
    "should call the provider" assertion silently pass for the wrong
    reason (a cache hit instead of a real fresh call)."""
    from attachments import ai_edit

    ai_edit._idempotency_cache.clear()
    yield
    ai_edit._idempotency_cache.clear()


class TestGetImageEditProvider:
    def test_raises_when_flag_is_off(self):
        settings = _settings(enable_image_editing=False, ima_api_key=SecretStr("k"))
        with pytest.raises(ImageEditNotConfiguredError):
            get_image_edit_provider(settings)

    def test_raises_when_key_is_missing(self):
        settings = _settings(enable_image_editing=True, ima_api_key=None)
        with pytest.raises(ImageEditNotConfiguredError):
            get_image_edit_provider(settings)

    def test_returns_a_real_provider_when_both_are_set(self):
        from media_gen.image_edit_provider import ImaImageEditProvider

        settings = _settings(enable_image_editing=True, ima_api_key=SecretStr("k"))
        provider = get_image_edit_provider(settings)
        assert isinstance(provider, ImaImageEditProvider)


class TestExecuteImageEditValidation:
    def test_rejects_an_unsupported_operation_before_any_provider_call(self):
        fake = FakeImageEditProvider()
        outcome = execute_image_edit(
            source_bytes=_png_bytes(),
            source_content_type="image/png",
            source_width=64,
            source_height=64,
            operation="delete_everything",
            prompt="do something",
            mask_bytes=None,
            settings=_BASE_SETTINGS,
            provider=fake,
        )
        assert outcome.status == "failed"
        assert outcome.error_code == "unsupported_operation"
        assert fake.requests == []

    def test_rejects_an_empty_prompt(self):
        fake = FakeImageEditProvider()
        outcome = execute_image_edit(
            source_bytes=_png_bytes(),
            source_content_type="image/png",
            source_width=64,
            source_height=64,
            operation="remove_object",
            prompt="   ",
            mask_bytes=None,
            settings=_BASE_SETTINGS,
            provider=fake,
        )
        assert outcome.status == "failed"
        assert outcome.error_code == "empty_prompt"

    def test_rejects_a_prompt_over_the_configured_length(self):
        fake = FakeImageEditProvider()
        outcome = execute_image_edit(
            source_bytes=_png_bytes(),
            source_content_type="image/png",
            source_width=64,
            source_height=64,
            operation="remove_object",
            prompt="x" * 51,  # settings cap is 50
            mask_bytes=None,
            settings=_BASE_SETTINGS,
            provider=fake,
        )
        assert outcome.status == "failed"
        assert outcome.error_code == "prompt_too_long"
        assert fake.requests == []

    def test_rate_limit_tripped_fails_closed_without_calling_the_provider(self):
        settings = _settings(media_gen_rate_limit=1, media_gen_rate_window_seconds=60.0)
        fake = FakeImageEditProvider()
        execute_image_edit(
            source_bytes=_png_bytes(),
            source_content_type="image/png",
            source_width=64,
            source_height=64,
            operation="remove_object",
            prompt="remove it",
            mask_bytes=None,
            settings=settings,
            provider=fake,
        )
        second = execute_image_edit(
            source_bytes=_png_bytes(),
            source_content_type="image/png",
            source_width=64,
            source_height=64,
            operation="remove_object",
            prompt="remove it again",
            mask_bytes=None,
            settings=settings,
            provider=fake,
        )
        assert second.status == "failed"
        assert second.error_code == "rate_limited"
        assert len(fake.requests) == 1  # only the first call actually reached the provider


def _run_edit(
    *,
    provider: FakeImageEditProvider,
    owner_subject: str | None,
    idempotency_key: str | None = None,
):
    """Calls `execute_image_edit` with a fixed source/operation/prompt,
    varying only the caller-identity/idempotency arguments each test below
    actually exercises -- explicit named parameters (not a `**dict`
    splat) so mypy can check each call against the real signature."""
    return execute_image_edit(
        source_bytes=_png_bytes(),
        source_content_type="image/png",
        source_width=64,
        source_height=64,
        operation="remove_object",
        prompt="remove it",
        mask_bytes=None,
        settings=_BASE_SETTINGS,
        provider=provider,
        owner_subject=owner_subject,
        idempotency_key=idempotency_key,
    )


class TestExecuteImageEditIdempotency:
    def test_same_key_same_owner_reuses_the_cached_outcome(self):
        fake = FakeImageEditProvider()
        first = _run_edit(provider=fake, owner_subject="alice", idempotency_key="key-1")
        second = _run_edit(provider=fake, owner_subject="alice", idempotency_key="key-1")
        assert first.status == second.status == "completed"
        assert len(fake.requests) == 1

    def test_same_key_different_owner_does_not_share_the_cache(self):
        """A cache keyed only by idempotency_key (ignoring the caller)
        would let one user's retry key collide with another's -- this
        proves ownership is part of the cache key."""
        fake = FakeImageEditProvider()
        _run_edit(provider=fake, owner_subject="alice", idempotency_key="shared-key")
        _run_edit(provider=fake, owner_subject="bob", idempotency_key="shared-key")
        assert len(fake.requests) == 2  # each owner's identical key triggered its own call

    def test_no_idempotency_key_never_caches(self):
        fake = FakeImageEditProvider()
        _run_edit(provider=fake, owner_subject="alice")
        _run_edit(provider=fake, owner_subject="alice")
        assert len(fake.requests) == 2
