"""Unit tests for attachments/context_builder.py."""

from __future__ import annotations

from attachments.context_builder import build_attachment_context
from attachments.models import ProcessedAttachment


def _processed(**overrides) -> ProcessedAttachment:
    base: ProcessedAttachment = {
        "attachment_id": "att_1",
        "filename": "doc.txt",
        "media_type": "text/plain",
        "source_type": "text",
        "extracted_text": "some content",
        "image_data_url": "",
        "chunks": [],
        "metadata": {},
        "processing_status": "succeeded",
        "error": "",
    }
    base.update(overrides)
    return base


class TestBuildAttachmentContext:
    def test_empty_list_returns_empty_string(self):
        context, truncated = build_attachment_context([], max_chars=1000)
        assert context == ""
        assert truncated is False

    def test_excludes_failed_attachments(self):
        items = [_processed(processing_status="failed", extracted_text="")]
        context, truncated = build_attachment_context(items, max_chars=1000)
        assert context == ""

    def test_excludes_attachments_with_no_extracted_text(self):
        items = [_processed(extracted_text="")]
        context, truncated = build_attachment_context(items, max_chars=1000)
        assert context == ""

    def test_includes_filename_and_type(self):
        items = [_processed(filename="resume.docx", source_type="docx")]
        context, _truncated = build_attachment_context(items, max_chars=1000)
        assert "resume.docx" in context
        assert "DOCX" in context
        assert "[ATTACHMENT 1]" in context
        assert "[END ATTACHMENTS]" in context

    def test_multiple_attachments_are_numbered(self):
        items = [
            _processed(attachment_id="att_1", filename="a.txt", extracted_text="content a"),
            _processed(attachment_id="att_2", filename="b.txt", extracted_text="content b"),
        ]
        context, _truncated = build_attachment_context(items, max_chars=1000)
        assert "[ATTACHMENT 1]" in context
        assert "[ATTACHMENT 2]" in context
        assert "content a" in context
        assert "content b" in context

    def test_deduplicates_identical_extracted_content(self):
        items = [
            _processed(attachment_id="att_1", filename="a.txt", extracted_text="same content"),
            _processed(attachment_id="att_2", filename="a-copy.txt", extracted_text="same content"),
        ]
        context, _truncated = build_attachment_context(items, max_chars=1000)
        assert "[ATTACHMENT 2]" not in context

    def test_truncates_and_flags_when_over_budget(self):
        items = [_processed(extracted_text="x" * 10_000)]
        context, truncated = build_attachment_context(items, max_chars=500)
        assert truncated is True
        assert "truncated" in context.lower()

    def test_splits_budget_fairly_across_attachments(self):
        items = [
            _processed(attachment_id="att_1", filename="a.txt", extracted_text="a" * 1000),
            _processed(attachment_id="att_2", filename="b.txt", extracted_text="b" * 1000),
        ]
        context, truncated = build_attachment_context(items, max_chars=1000)
        assert truncated is True
        # Neither attachment should have consumed the whole budget alone.
        assert context.count("a") < 900
        assert context.count("b") < 900

    def test_never_includes_failed_attachment_as_if_it_succeeded(self):
        items = [
            _processed(attachment_id="att_1", extracted_text="good content"),
            _processed(attachment_id="att_2", processing_status="failed", extracted_text=""),
        ]
        context, _truncated = build_attachment_context(items, max_chars=1000)
        assert "[ATTACHMENT 2]" not in context
        assert context.count("[ATTACHMENT") == 1
