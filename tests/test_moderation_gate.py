"""Unit tests for moderation/gate.py -- the aggregation/decision logic:
a hard-reject on any chunk blocks the whole asset, a soft-flag alone still
passes, and the audit log never carries content. `moderation.provider
.analyze_chunk` and the blocklist are mocked; no real Azure call or file
I/O happens.
"""

from __future__ import annotations

from config.settings import Settings
from moderation.gate import decision_summary, moderate_chunks
from moderation.types import CategoryResult, ModerationChunk


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"moderation_severity_threshold": 4}
    base.update(overrides)
    return Settings(**base)


def _provider_returning(*results: CategoryResult):
    return lambda chunk, settings: list(results)


class TestModerateChunks:
    def test_passes_when_nothing_triggers(self, monkeypatch):
        monkeypatch.setattr("moderation.gate.load_blocklist", lambda path: {})
        monkeypatch.setattr(
            "moderation.gate.analyze_chunk",
            _provider_returning(
                CategoryResult(category="hate", triggered=False, severity=0),
                CategoryResult(category="violence", triggered=False, severity=0),
            ),
        )
        chunks = [ModerationChunk(chunk_index=0, content_type="text", text="hello world")]

        decision = moderate_chunks("hash123", chunks, _settings())

        assert decision.passed
        assert decision.triggering_categories == ()

    def test_severity_at_or_above_threshold_triggers_hard_reject(self, monkeypatch):
        monkeypatch.setattr("moderation.gate.load_blocklist", lambda path: {})
        monkeypatch.setattr(
            "moderation.gate.analyze_chunk",
            _provider_returning(CategoryResult(category="violence", triggered=False, severity=4)),
        )
        chunks = [ModerationChunk(chunk_index=0, content_type="image", text=None)]

        decision = moderate_chunks("hash123", chunks, _settings(moderation_severity_threshold=4))

        assert not decision.passed
        assert decision.triggering_categories == ("violence",)
        assert decision.triggering_chunk_index == 0

    def test_severity_below_threshold_does_not_trigger(self, monkeypatch):
        monkeypatch.setattr("moderation.gate.load_blocklist", lambda path: {})
        monkeypatch.setattr(
            "moderation.gate.analyze_chunk",
            _provider_returning(CategoryResult(category="violence", triggered=False, severity=2)),
        )
        chunks = [ModerationChunk(chunk_index=0, content_type="image", text=None)]

        decision = moderate_chunks("hash123", chunks, _settings(moderation_severity_threshold=4))

        assert decision.passed

    def test_any_chunk_hard_rejecting_blocks_the_whole_asset(self, monkeypatch):
        """Two chunks -- only the second triggers -- the asset is still
        rejected as a whole, not partially ingested."""
        monkeypatch.setattr("moderation.gate.load_blocklist", lambda path: {})

        def _fake_analyze(chunk, settings):
            if chunk.chunk_index == 0:
                return [CategoryResult(category="hate", triggered=False, severity=0)]
            return [CategoryResult(category="hate", triggered=False, severity=6)]

        monkeypatch.setattr("moderation.gate.analyze_chunk", _fake_analyze)
        chunks = [
            ModerationChunk(chunk_index=0, content_type="text", text="page one"),
            ModerationChunk(chunk_index=1, content_type="text", text="page two"),
        ]

        decision = moderate_chunks("hash123", chunks, _settings())

        assert not decision.passed
        assert decision.triggering_chunk_index == 1
        # every chunk is still checked, not short-circuited on the first hit
        assert len(decision.chunk_results) == 2

    def test_blocklist_match_hard_rejects_even_with_a_clean_provider_result(self, monkeypatch):
        monkeypatch.setattr("moderation.gate.load_blocklist", lambda path: {"weapons": ["handgun"]})
        monkeypatch.setattr(
            "moderation.gate.analyze_chunk",
            _provider_returning(CategoryResult(category="violence", triggered=False, severity=0)),
        )
        chunks = [ModerationChunk(chunk_index=0, content_type="text", text="a handgun was found")]

        decision = moderate_chunks("hash123", chunks, _settings())

        assert not decision.passed
        assert decision.triggering_categories == ("weapons",)

    def test_synthetic_media_soft_flag_never_blocks_ingestion(self, monkeypatch):
        """An image chunk always gets a not_checked synthetic_media entry
        -- it must never contribute to a hard-reject even though it's
        `triggered=False` by construction."""
        monkeypatch.setattr("moderation.gate.load_blocklist", lambda path: {})
        monkeypatch.setattr(
            "moderation.gate.analyze_chunk",
            _provider_returning(CategoryResult(category="hate", triggered=False, severity=0)),
        )
        chunks = [ModerationChunk(chunk_index=0, content_type="image", text=None)]

        decision = moderate_chunks("hash123", chunks, _settings())

        assert decision.passed
        categories_checked = {r.category for r in decision.chunk_results[0].categories}
        assert "synthetic_media" in categories_checked

    def test_audit_log_never_carries_chunk_content(self, monkeypatch):
        captured = {}

        def _fake_log_event(event_type, severity, detail, **context):
            captured["event_type"] = event_type
            captured["context"] = context

        monkeypatch.setattr("moderation.gate.log_security_event", _fake_log_event)
        monkeypatch.setattr("moderation.gate.load_blocklist", lambda path: {})
        monkeypatch.setattr(
            "moderation.gate.analyze_chunk",
            _provider_returning(CategoryResult(category="hate", triggered=False, severity=6)),
        )
        secret_text = "this exact sentence must never reach the audit log"
        chunks = [ModerationChunk(chunk_index=0, content_type="text", text=secret_text)]

        moderate_chunks("hash123", chunks, _settings())

        assert captured["event_type"] == "content_moderation_rejected"
        rendered = repr(captured["context"])
        assert secret_text not in rendered
        assert captured["context"]["asset_hash"] == "hash123"


class TestDecisionSummary:
    def test_is_json_serializable(self, monkeypatch):
        monkeypatch.setattr("moderation.gate.load_blocklist", lambda path: {})
        monkeypatch.setattr(
            "moderation.gate.analyze_chunk",
            _provider_returning(CategoryResult(category="hate", triggered=False, severity=6)),
        )
        chunks = [ModerationChunk(chunk_index=0, content_type="text", text="x")]
        decision = moderate_chunks("hash123", chunks, _settings())

        import json

        summary = decision_summary(decision)
        json.dumps(summary)  # must not raise
        assert summary["status"] == "rejected"
        assert summary["triggering_categories"] == ["hate"]
