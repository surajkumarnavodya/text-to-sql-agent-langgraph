"""Unit tests for security/audit_log.py (item H: security-focused audit
logging)."""

from __future__ import annotations

import logging

from security.audit_log import log_security_event


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
