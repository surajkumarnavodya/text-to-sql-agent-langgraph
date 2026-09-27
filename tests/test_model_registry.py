"""Unit tests for `agent/model_registry.py` and `config/ollama_models.py` --
the configuration-driven Ollama model registry backing `GET /models` and
`AskRequest.model` (see docs/CONFIGURATION.md's "Model selection" section).

Fully mocked -- no real Ollama server or network call. `discover_installed_models`
is exercised via monkeypatching `agent.model_registry.get_ollama_client`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.model_registry import (
    InvalidModelSelectionError,
    build_model_options,
    discover_installed_models,
    installed_model_names,
    validate_model_selection,
)
from config.ollama_models import describe_model, load_ollama_model_catalog
from config.settings import Settings
from security.secrets import SecretStr


def _settings(**overrides: object) -> Settings:
    base = dict(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        ollama_request_timeout_seconds=60,
        ollama_allowed_models=("llama3.1:8b", "qwen2.5:7b", "mistral:7b"),
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
        chroma_collection_name="x",
        embedding_model_name="x",
    )
    base.update(overrides)
    return Settings(**base)


class FakeOllamaClient:
    def __init__(self, list_response: object) -> None:
        self._list_response = list_response

    def list(self) -> object:
        return self._list_response


class TestInstalledModelNames:
    def test_extracts_names_from_dict_shape(self):
        response = {"models": [{"model": "llama3.1:8b"}, {"name": "qwen2.5:7b"}]}
        assert installed_model_names(response) == {"llama3.1:8b", "qwen2.5:7b"}

    def test_extracts_names_from_object_shape(self):
        class _Model:
            def __init__(self, model):
                self.model = model

        class _ListResponse:
            models = [_Model("llama3.1:8b"), _Model("mistral:7b")]

        assert installed_model_names(_ListResponse()) == {"llama3.1:8b", "mistral:7b"}

    def test_unrecognized_shape_returns_empty_set_not_an_error(self):
        assert installed_model_names(object()) == set()
        assert installed_model_names(None) == set()

    def test_missing_models_key_returns_empty_set(self):
        assert installed_model_names({}) == set()


class TestDiscoverInstalledModels:
    def test_returns_installed_names_from_ollama_client(self, monkeypatch):
        settings = _settings()
        monkeypatch.setattr(
            "agent.model_registry.get_ollama_client",
            lambda s: FakeOllamaClient({"models": [{"model": "llama3.1:8b"}]}),
        )
        assert discover_installed_models(settings) == {"llama3.1:8b"}

    def test_fails_open_to_empty_set_when_ollama_unreachable(self, monkeypatch):
        settings = _settings()

        def _raise(s):
            raise ConnectionError("refused")

        monkeypatch.setattr("agent.model_registry.get_ollama_client", _raise)
        assert discover_installed_models(settings) == set()


class TestValidateModelSelection:
    def test_none_resolves_to_the_configured_default(self):
        settings = _settings(ollama_model="llama3.1:8b")
        assert validate_model_selection(None, settings) == "llama3.1:8b"

    def test_allowed_model_is_returned_unchanged(self):
        settings = _settings()
        assert validate_model_selection("qwen2.5:7b", settings) == "qwen2.5:7b"

    def test_disallowed_model_raises(self):
        settings = _settings()
        with pytest.raises(InvalidModelSelectionError):
            validate_model_selection("some-random-model:99b", settings)

    def test_disallowed_model_error_names_the_allowed_set(self):
        settings = _settings(ollama_allowed_models=("llama3.1:8b", "qwen2.5:7b"))
        with pytest.raises(InvalidModelSelectionError) as exc_info:
            validate_model_selection("gpt-4", settings)
        assert "llama3.1:8b" in str(exc_info.value)
        assert "qwen2.5:7b" in str(exc_info.value)

    def test_disallowed_model_never_reaches_ollama(self, monkeypatch):
        """Rule 7 (no arbitrary model selection without backend validation):
        an invalid model must be rejected before any Ollama call is made."""
        settings = _settings()

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("must not call Ollama for a disallowed model")

        monkeypatch.setattr("agent.model_registry.get_ollama_client", _fail_if_called)
        with pytest.raises(InvalidModelSelectionError):
            validate_model_selection("not-allowed:1b", settings)


class TestBuildModelOptions:
    def test_every_allowed_model_appears_exactly_once(self, monkeypatch):
        settings = _settings(ollama_allowed_models=("llama3.1:8b", "qwen2.5:7b", "mistral:7b"))
        options = build_model_options(settings, installed=set())
        assert [o.id for o in options] == ["llama3.1:8b", "qwen2.5:7b", "mistral:7b"]

    def test_default_model_is_flagged(self, monkeypatch):
        settings = _settings(ollama_model="qwen2.5:7b")
        options = build_model_options(settings, installed=set())
        by_id = {o.id: o for o in options}
        assert by_id["qwen2.5:7b"].is_default is True
        assert by_id["llama3.1:8b"].is_default is False

    def test_installed_and_available_reflect_the_passed_in_set(self):
        settings = _settings()
        options = build_model_options(settings, installed={"llama3.1:8b"})
        by_id = {o.id: o for o in options}
        assert by_id["llama3.1:8b"].installed is True
        assert by_id["llama3.1:8b"].available is True
        assert by_id["qwen2.5:7b"].installed is False
        assert by_id["qwen2.5:7b"].available is False

    def test_uninstalled_model_is_never_dropped_only_flagged(self):
        """A configured-but-not-installed model must still be listed (so the
        UI can show it as 'not installed' rather than silently hiding it)."""
        settings = _settings()
        options = build_model_options(settings, installed=set())
        assert len(options) == 3
        assert all(o.installed is False for o in options)

    def test_none_installed_triggers_live_discovery(self, monkeypatch):
        monkeypatch.setattr(
            "agent.model_registry.get_ollama_client",
            lambda s: FakeOllamaClient({"models": [{"model": "llama3.1:8b"}]}),
        )
        settings = _settings()
        options = build_model_options(settings)  # installed=None -> live discovery
        by_id = {o.id: o for o in options}
        assert by_id["llama3.1:8b"].installed is True

    def test_curated_metadata_is_attached_when_available(self):
        settings = _settings(ollama_allowed_models=("llama3.1:8b",))
        options = build_model_options(settings, installed=set())
        assert options[0].display_name == "Llama 3.1 8B"
        assert "sql" in options[0].capabilities

    def test_unknown_model_gets_a_generic_fallback_not_dropped(self):
        """An operator can list any model name -- one with no curated
        config/ollama_models.yaml entry must still show up sensibly."""
        settings = _settings(
            ollama_model="totally-unknown-model:99b",
            ollama_allowed_models=("totally-unknown-model:99b",),
        )
        options = build_model_options(settings, installed=set())
        assert len(options) == 1
        assert options[0].display_name  # non-empty fallback, not a crash
        assert "sql" in options[0].capabilities


class TestOllamaModelCatalog:
    def test_load_real_catalog_includes_the_default_model(self):
        catalog = load_ollama_model_catalog()
        assert "llama3.1:8b" in catalog
        assert catalog["llama3.1:8b"].display_name

    def test_missing_file_returns_empty_dict_not_an_error(self, tmp_path):
        catalog = load_ollama_model_catalog(tmp_path / "does-not-exist.yaml")
        assert catalog == {}

    def test_describe_model_returns_curated_entry_when_present(self):
        catalog = load_ollama_model_catalog()
        info = describe_model("llama3.1:8b", catalog)
        assert info.display_name == "Llama 3.1 8B"

    def test_describe_model_falls_back_gracefully_for_unknown_id(self):
        info = describe_model("some-new-model:20b", {})
        assert info.id == "some-new-model:20b"
        assert info.display_name  # never empty
        assert "sql" in info.capabilities
        assert info.recommended is False
