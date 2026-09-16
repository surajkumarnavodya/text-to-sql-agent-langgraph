"""Unit tests for config/settings.py's security-relevant validation and
SecretStr wiring, added by the enterprise security audit (item M: security
configuration validation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import ConfigurationError, Settings, get_settings
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
    `BaseModel.model_copy(update=...)` (this file in particular relies on
    the rebuild re-running full field validation, since every test in
    `TestPositiveValueValidation` et al. expects a bad override to raise)."""
    return Settings(**{**_BASE_SETTINGS.__dict__, **overrides})


class TestSecretFieldCoercion:
    def test_db_password_is_wrapped_in_secretstr(self):
        settings = _settings()
        assert isinstance(settings.db_password, SecretStr)
        # Pydantic's SecretStr, unlike the old hand-rolled one, doesn't
        # compare equal to the raw string -- see tests/test_secrets.py's
        # `test_equality_compares_the_wrapped_value_not_the_raw_string`.
        assert settings.db_password.get_secret_value() == "S3cr3t!"

    def test_db_connection_string_is_wrapped_in_secretstr(self):
        settings = _settings(db_connection_string="postgresql://reader:pw@host/db")
        assert isinstance(settings.db_connection_string, SecretStr)

    def test_none_password_stays_none(self):
        settings = _settings(db_password=None)
        assert settings.db_password is None

    def test_settings_repr_never_contains_the_password(self):
        settings = _settings()
        assert "S3cr3t!" not in repr(settings)


class TestPositiveValueValidation:
    @pytest.mark.parametrize(
        "field",
        [
            "max_retries",
            "max_result_rows",
            "query_timeout_seconds",
            "llm_max_tokens",
            "insight_max_tokens",
            "max_question_length",
            "question_rate_limit_per_minute",
            "llm_call_rate_limit_per_minute",
            "cost_estimation_timeout_seconds",
            "cost_moderate_row_threshold",
            "query_plan_max_tokens",
            "sql_review_max_tokens",
        ],
    )
    @pytest.mark.parametrize("bad_value", [0, -1, -100])
    def test_non_positive_value_raises(self, field, bad_value):
        with pytest.raises(ConfigurationError, match="greater than 0"):
            _settings(**{field: bad_value})

    def test_valid_settings_do_not_raise(self):
        _settings()  # should not raise


class TestComplexQueryMaxRetryBonusValidation:
    """Unlike the plain `TestPositiveValueValidation` fields above, `0` is a
    deliberate, valid value here (disables the adaptive retry budget -- see
    `agent/complexity.py`); only a negative value is a misconfiguration."""

    def test_zero_is_accepted(self):
        settings = _settings(complex_query_max_retry_bonus=0)
        assert settings.complex_query_max_retry_bonus == 0

    def test_positive_is_accepted(self):
        settings = _settings(complex_query_max_retry_bonus=5)
        assert settings.complex_query_max_retry_bonus == 5

    @pytest.mark.parametrize("bad_value", [-1, -100])
    def test_negative_raises(self, bad_value):
        with pytest.raises(ConfigurationError, match="COMPLEX_QUERY_MAX_RETRY_BONUS"):
            _settings(complex_query_max_retry_bonus=bad_value)


class TestCostThresholdOrdering:
    def test_moderate_below_high_is_accepted(self):
        _settings(cost_moderate_row_threshold=100, cost_high_row_threshold=1000)

    def test_moderate_equal_to_high_is_rejected(self):
        with pytest.raises(ConfigurationError, match="strictly less than"):
            _settings(cost_moderate_row_threshold=1000, cost_high_row_threshold=1000)

    def test_moderate_above_high_is_rejected(self):
        with pytest.raises(ConfigurationError, match="strictly less than"):
            _settings(cost_moderate_row_threshold=2000, cost_high_row_threshold=1000)


class TestAuthModeAndOidcValidation:
    """2026 Phase 2 security review: `Settings.auth_mode`'s derivation and
    the OIDC-specific `model_validator`s that guard against the two
    concrete JWT misconfiguration classes named in `docs/AUTHENTICATION.md`
    (audience-less issuer trust, and an `alg` allowlist that could accept
    `none`)."""

    def test_auth_mode_is_none_when_nothing_configured(self):
        settings = _settings()
        assert settings.auth_mode == "none"

    def test_auth_mode_is_static_token_when_only_api_auth_token_set(self):
        settings = _settings(api_auth_token=SecretStr("s3cret"))
        assert settings.auth_mode == "static_token"

    def test_auth_mode_is_oidc_when_issuer_configured(self):
        settings = _settings(
            oidc_issuer="https://idp.example.com/",
            oidc_audience="my-api",
        )
        assert settings.auth_mode == "oidc"

    def test_auth_mode_is_oidc_even_with_a_static_token_also_configured(self):
        """Both may be configured together (see api/auth.py's docstring) --
        `auth_mode` still reports "oidc" as the primary mode."""
        settings = _settings(
            oidc_issuer="https://idp.example.com/",
            oidc_audience="my-api",
            api_auth_token=SecretStr("s3cret"),
        )
        assert settings.auth_mode == "oidc"

    def test_oidc_issuer_without_audience_raises(self):
        with pytest.raises(ConfigurationError, match="OIDC_AUDIENCE"):
            _settings(oidc_issuer="https://idp.example.com/", oidc_audience=None)

    def test_empty_oidc_algorithms_raises(self):
        with pytest.raises(ConfigurationError, match="OIDC_ALGORITHMS"):
            _settings(
                oidc_issuer="https://idp.example.com/",
                oidc_audience="my-api",
                oidc_algorithms=(),
            )

    def test_oidc_algorithms_containing_none_raises(self):
        with pytest.raises(ConfigurationError, match="alg confusion"):
            _settings(
                oidc_issuer="https://idp.example.com/",
                oidc_audience="my-api",
                oidc_algorithms=("RS256", "none"),
            )

    def test_oidc_algorithms_none_case_insensitive_raises(self):
        with pytest.raises(ConfigurationError, match="alg confusion"):
            _settings(
                oidc_issuer="https://idp.example.com/",
                oidc_audience="my-api",
                oidc_algorithms=("None",),
            )


class TestProductionRequiresIdentity:
    """`ENVIRONMENT=production` must refuse to start with `auth_mode ==
    "none"` -- the fail-closed guarantee `docs/AUTHENTICATION.md` documents."""

    def test_development_with_no_auth_is_accepted(self):
        settings = _settings(environment="development")
        assert settings.auth_mode == "none"

    def test_production_with_no_auth_raises(self):
        with pytest.raises(ConfigurationError, match="ENVIRONMENT=production"):
            _settings(environment="production")

    def test_production_with_static_token_is_accepted(self):
        settings = _settings(environment="production", api_auth_token=SecretStr("s3cret"))
        assert settings.auth_mode == "static_token"

    def test_production_with_oidc_is_accepted(self):
        settings = _settings(
            environment="production",
            oidc_issuer="https://idp.example.com/",
            oidc_audience="my-api",
        )
        assert settings.auth_mode == "oidc"

    def test_unknown_environment_value_raises(self):
        with pytest.raises(ConfigurationError):
            _settings(environment="staging")


class TestLogRedactionLevelValidation:
    @pytest.mark.parametrize("level", ["standard", "strict"])
    def test_known_level_is_accepted(self, level):
        settings = _settings(log_redaction_level=level)
        assert settings.log_redaction_level == level

    def test_unknown_level_raises(self):
        with pytest.raises(ConfigurationError, match="LOG_REDACTION_LEVEL"):
            _settings(log_redaction_level="verbose")


class TestMultiDatabaseConfig:
    """`Settings.databases` -- the named-connection list `db.connection.
    get_connection` and `embeddings.retriever.select_database` read from.
    See `config.settings._parse_named_connections` and `Settings.
    __post_init__`'s fallback."""

    def test_no_db_connections_falls_back_to_one_default_entry_matching_flat_fields(self):
        settings = _settings()

        assert [c.name for c in settings.databases] == ["default"]
        default = settings.databases[0]
        assert default.db_type == settings.db_type
        assert default.db_host == settings.db_host
        assert default.db_name == settings.db_name
        assert default.db_user == settings.db_user
        assert default.db_password == settings.db_password

    def test_directly_constructed_settings_without_databases_still_gets_a_default(self):
        """`_fill_default_database` (a `model_validator(mode="after")`) runs
        on *every* construction -- not just `get_settings()` -- so a
        hand-built `Settings(...)` in a test (or any other caller) never has
        an empty `.databases`.

        `databases=()` is passed explicitly alongside the overrides: `_BASE_
        SETTINGS` already has its own (postgresql-flavored) `.databases`
        baked in from its own construction, and `_settings()`'s rebuild only
        overwrites the fields named in **overrides -- without resetting
        `databases` too, the stale postgresql entry would be copied over
        unchanged even though `db_type` below is being changed to mysql.
        """
        settings = _settings(db_type="mysql", db_host="mysql-host", databases=())
        assert len(settings.databases) == 1
        assert settings.databases[0].db_type == "mysql"

    def test_db_connections_env_parses_named_connections(self, monkeypatch):
        monkeypatch.setenv("DB_CONNECTIONS", "sales,hr")
        monkeypatch.setenv("DB_SALES_TYPE", "postgresql")
        monkeypatch.setenv("DB_SALES_HOST", "sales-host")
        monkeypatch.setenv("DB_SALES_NAME", "salesdb")
        monkeypatch.setenv("DB_HR_TYPE", "mysql")
        monkeypatch.setenv("DB_HR_HOST", "hr-host")
        monkeypatch.setenv("DB_HR_NAME", "hrdb")
        get_settings.cache_clear()
        try:
            settings = get_settings()
            names = [c.name for c in settings.databases]
            assert names == ["sales", "hr"]
            sales = settings.databases[0]
            assert sales.db_type == "postgresql"
            assert sales.db_host == "sales-host"
            assert sales.db_name == "salesdb"
            hr = settings.databases[1]
            assert hr.db_type == "mysql"
            assert hr.db_host == "hr-host"
        finally:
            get_settings.cache_clear()

    def test_db_connections_entry_missing_type_raises(self, monkeypatch):
        monkeypatch.setenv("DB_CONNECTIONS", "sales")
        monkeypatch.delenv("DB_SALES_TYPE", raising=False)
        get_settings.cache_clear()
        try:
            with pytest.raises(ConfigurationError, match="DB_SALES_TYPE"):
                get_settings()
        finally:
            get_settings.cache_clear()

    def test_db_connections_names_colliding_on_the_same_env_prefix_raises(self, monkeypatch):
        # "sales-east" and "sales_east" both normalize to DB_SALES_EAST_*.
        monkeypatch.setenv("DB_CONNECTIONS", "sales-east,sales_east")
        monkeypatch.setenv("DB_SALES_EAST_TYPE", "postgresql")
        get_settings.cache_clear()
        try:
            with pytest.raises(ConfigurationError, match="same env prefix"):
                get_settings()
        finally:
            get_settings.cache_clear()
