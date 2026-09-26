"""Unit tests for attachments/models.py."""

from __future__ import annotations

from attachments.models import Attachment, AttachmentError, AttachmentErrorCode


class TestAttachment:
    def test_default_processing_status_is_pending(self):
        att = Attachment(
            original_filename="a.txt",
            safe_filename="a.txt",
            media_type="text/plain",
            extension=".txt",
            size_bytes=10,
            sha256="abc",
        )
        assert att.processing_status == "pending"
        assert att.extracted_text is None
        assert att.owner_subject is None

    def test_with_processing_result_returns_new_instance(self):
        att = Attachment(
            original_filename="a.txt",
            safe_filename="a.txt",
            media_type="text/plain",
            extension=".txt",
            size_bytes=10,
            sha256="abc",
        )
        updated = att.with_processing_result(status="succeeded", extracted_text="hello")

        assert att.processing_status == "pending"  # original untouched
        assert updated.processing_status == "succeeded"
        assert updated.extracted_text == "hello"
        assert updated.attachment_id == att.attachment_id  # same identity

    def test_is_frozen(self):
        att = Attachment(
            original_filename="a.txt",
            safe_filename="a.txt",
            media_type="text/plain",
            extension=".txt",
            size_bytes=10,
            sha256="abc",
        )
        try:
            att.processing_status = "succeeded"  # type: ignore[misc]
            raised = False
        except Exception:
            raised = True
        assert raised

    def test_attachment_ids_are_unique(self):
        kwargs = dict(
            original_filename="a.txt",
            safe_filename="a.txt",
            media_type="text/plain",
            extension=".txt",
            size_bytes=10,
            sha256="abc",
        )
        first = Attachment(**kwargs)
        second = Attachment(**kwargs)
        assert first.attachment_id != second.attachment_id


class TestAttachmentError:
    def test_serializes_stable_code(self):
        error = AttachmentError(
            code=AttachmentErrorCode.FILE_TOO_LARGE, filename="big.pdf", message="Too big."
        )
        dumped = error.model_dump(mode="json")
        assert dumped["code"] == "FILE_TOO_LARGE"
        assert dumped["filename"] == "big.pdf"
