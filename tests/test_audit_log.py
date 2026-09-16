"""Unit tests for security/audit_log.py (item H: security-focused audit
logging)."""

from __future__ import annotations

import logging

from security.audit_log import (
    CorrelationIdLogFilter,
    log_security_event,
    reset_correlation_id,
    set_correlation_id,
)


class TestLogSecurityEvent:
    def test_emits_one_structured_line_on_the_dedicated_logger(self, caplog):
        with caplog.at_level(logging.INFO, logger="security.audit"):
            log_security_event("input_rejected", "info", "test detail", reason="too_long")

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.name == "security.audit"
        assert "event=input_rejected" in record.message
        assert "severity=info" in record.message
        assert "reason=" in record.message
        assert "too_long" in record.message

    def test_severity_maps_to_the_matching_log_level(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="security.audit"):
            log_security_event("sql_safety_violation", "warning", "blocked")
        assert caplog.records[0].levelno == logging.WARNING

    def test_context_values_are_truncated(self, caplog):
        huge = "x" * 10_000
        with caplog.at_level(logging.INFO, logger="security.audit"):
            log_security_event("possible_rag_poisoning", "warning", "detail", payload=huge)
        message = caplog.records[0].message
        assert len(message) < 1000  # nowhere near the 10,000-char raw value

    def test_never_raises_even_on_an_internal_error(self):
        """A logging bug must not break the caller -- log_security_event
        swallows its own internal errors rather than propagating them."""

        class _Unrepresentable:
            def __repr__(self):
                raise RuntimeError("boom")

        # Must not raise.
        log_security_event("input_rejected", "info", "detail", bad=_Unrepresentable())

    def test_detail_is_included_in_the_message(self, caplog):
        with caplog.at_level(logging.INFO, logger="security.audit"):
            log_security_event("rate_limit_tripped", "info", "the limiter tripped")
        assert "the limiter tripped" in caplog.records[0].message


class TestCorrelationIdLogFilter:
    """2026 Phase 3 observability gap: before this filter existed, a
    request's correlation ID only showed up on `security.audit` events --
    an ordinary `logging.getLogger(__name__).info(...)` call anywhere else
    (agent/nodes.py, rag/, db/, media_gen/, search/) carried none. This
    filter, attached to the root logger by `config.settings.configure_logging`,
    stamps `record.correlation_id` on *every* record so any module's plain
    logger becomes correlation-ID-aware with zero changes to that module."""

    def test_stamps_the_current_correlation_id_onto_the_record(self):
        token = set_correlation_id("req-abc-123")
        try:
            record = logging.LogRecord("some.module", logging.INFO, __file__, 1, "msg", (), None)
            result = CorrelationIdLogFilter().filter(record)
        finally:
            reset_correlation_id(token)

        assert result is True
        assert record.correlation_id == "req-abc-123"

    def test_stamps_a_placeholder_when_no_request_is_in_flight(self):
        record = logging.LogRecord("some.module", logging.INFO, __file__, 1, "msg", (), None)
        CorrelationIdLogFilter().filter(record)
        assert record.correlation_id == "-"

    def test_ordinary_module_logger_gains_correlation_id_via_caplog(self, caplog):
        """End-to-end: a plain per-module logger (not `security.audit`)
        picks up the correlation ID once the filter is attached, exactly as
        `configure_logging` wires it onto the root handler."""
        module_logger = logging.getLogger("some.arbitrary.module")
        correlation_filter = CorrelationIdLogFilter()
        module_logger.addFilter(correlation_filter)
        token = set_correlation_id("req-xyz-789")
        try:
            with caplog.at_level(logging.INFO, logger="some.arbitrary.module"):
                module_logger.info("doing work")
        finally:
            reset_correlation_id(token)
            module_logger.removeFilter(correlation_filter)

        assert caplog.records[0].correlation_id == "req-xyz-789"
