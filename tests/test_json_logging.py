"""Unit tests for `config.settings._JsonLogFormatter`/`configure_logging`'s
`Settings.log_format` -- enterprise scalability/security assessment
(2026-09-27): opt-in structured JSON logging for real log-aggregation
pipelines, with the pre-existing text format kept as the unchanged default.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from config.settings import Settings, _JsonLogFormatter, configure_logging
from security.audit_log import (
    reset_audit_tenant_id,
    reset_correlation_id,
    set_audit_tenant_id,
    set_correlation_id,
)
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
    chroma_collection_name="x",
    embedding_model_name="x",
)


class TestLogFormatSetting:
    def test_defaults_to_text(self):
        assert _BASE_SETTINGS.log_format == "text"

    def test_accepts_json(self):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "log_format": "json"})
        assert settings.log_format == "json"


class TestJsonLogFormatter:
    def _record(self, message: str = "hello", exc_info=None) -> logging.LogRecord:
        record = logging.LogRecord(
            name="some.module",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=exc_info,
        )
        return record

    def test_emits_valid_json(self):
        record = self._record()
        record.correlation_id = "req-123"
        record.tenant_id = "tenant-a"
        rendered = _JsonLogFormatter().format(record)

        parsed = json.loads(rendered)  # must not raise
        assert parsed["message"] == "hello"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "some.module"
        assert parsed["correlation_id"] == "req-123"
        assert parsed["tenant_id"] == "tenant-a"
        assert "timestamp" in parsed

    def test_missing_correlation_id_renders_as_null_not_a_crash(self):
        record = self._record()
        # No `correlation_id`/`tenant_id` attribute set -- simulates a
        # record that bypassed CorrelationIdLogFilter/TenantIdLogFilter
        # (shouldn't happen in practice, but the formatter must not crash
        # on it).
        rendered = _JsonLogFormatter().format(record)
        parsed = json.loads(rendered)
        assert parsed["correlation_id"] is None
        assert parsed["tenant_id"] is None

    def test_placeholder_dash_correlation_id_renders_as_null(self):
        """CorrelationIdLogFilter stamps '-' outside a request -- the JSON
        formatter should render that as a real null, not the literal
        string '-', so a log-pipeline query for "no correlation id" works
        naturally (`correlation_id IS NULL`, not `correlation_id = '-'`).
        Prompt 23: `TenantIdLogFilter` makes the identical choice for
        `tenant_id`."""
        record = self._record()
        record.correlation_id = "-"
        record.tenant_id = "-"
        rendered = _JsonLogFormatter().format(record)
        parsed = json.loads(rendered)
        assert parsed["correlation_id"] is None
        assert parsed["tenant_id"] is None

    def test_exception_info_is_included_when_present(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = self._record(exc_info=sys.exc_info())
        rendered = _JsonLogFormatter().format(record)
        parsed = json.loads(rendered)
        assert "boom" in parsed["exception"]

    def test_no_exception_key_when_absent(self):
        record = self._record()
        rendered = _JsonLogFormatter().format(record)
        assert "exception" not in json.loads(rendered)

    def test_message_with_percent_formatting_args_is_resolved(self):
        record = logging.LogRecord(
            name="some.module",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="value=%s count=%d",
            args=("x", 5),
            exc_info=None,
        )
        rendered = _JsonLogFormatter().format(record)
        assert json.loads(rendered)["message"] == "value=x count=5"


class TestConfigureLoggingJsonFormat:
    def test_json_format_attaches_the_json_formatter(self, monkeypatch):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "log_format": "json"})
        monkeypatch.setattr("config.settings.get_settings", lambda: settings)

        configure_logging()

        formatters = [h.formatter for h in logging.getLogger().handlers]
        assert any(isinstance(f, _JsonLogFormatter) for f in formatters)

    def test_text_format_does_not_attach_the_json_formatter(self, monkeypatch):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "log_format": "text"})
        monkeypatch.setattr("config.settings.get_settings", lambda: settings)

        configure_logging()

        formatters = [h.formatter for h in logging.getLogger().handlers]
        assert not any(isinstance(f, _JsonLogFormatter) for f in formatters)

    def test_end_to_end_json_line_is_parseable_via_caplog_capture(self, monkeypatch, capsys):
        settings = Settings(**{**_BASE_SETTINGS.__dict__, "log_format": "json"})
        monkeypatch.setattr("config.settings.get_settings", lambda: settings)
        configure_logging()

        correlation_token = set_correlation_id("req-e2e-1")
        tenant_token = set_audit_tenant_id("tenant-e2e")
        try:
            logging.getLogger("test.json.e2e").warning("something happened")
        finally:
            reset_correlation_id(correlation_token)
            reset_audit_tenant_id(tenant_token)

        captured = capsys.readouterr()
        # The formatter emits one JSON object per line -- find the one this
        # test just produced among whatever else landed on stderr.
        matching_lines = [
            line for line in captured.err.splitlines() if "something happened" in line
        ]
        assert matching_lines, "expected the JSON log line on stderr"
        parsed = json.loads(matching_lines[-1])
        assert parsed["message"] == "something happened"
        assert parsed["level"] == "WARNING"
        assert parsed["correlation_id"] == "req-e2e-1"
        assert parsed["tenant_id"] == "tenant-e2e"
