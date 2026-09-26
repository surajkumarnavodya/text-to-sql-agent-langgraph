"""Unit tests for attachments/validation.py."""

from __future__ import annotations

from attachments.models import AttachmentErrorCode
from attachments.validation import compute_sha256, validate_upload
from config.settings import Settings

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 100


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


class TestValidateUpload:
    def test_accepts_a_valid_image(self):
        result = validate_upload("cat.png", "image/png", _PNG_BYTES, _settings())
        assert result.valid
        assert result.errors == []

    def test_rejects_empty_file(self):
        result = validate_upload("empty.txt", "text/plain", b"", _settings())
        assert not result.valid
        assert result.errors[0].code == AttachmentErrorCode.EMPTY_FILE

    def test_rejects_unsupported_extension(self):
        result = validate_upload("virus.exe", None, b"MZ" + b"0" * 20, _settings())
        assert not result.valid
        assert result.errors[0].code == AttachmentErrorCode.UNSUPPORTED_FILE_TYPE

    def test_rejects_legacy_office_formats_with_a_helpful_message(self):
        result = validate_upload("old.doc", None, b"\xd0\xcf\x11\xe0" + b"0" * 20, _settings())
        assert not result.valid
        assert result.errors[0].code == AttachmentErrorCode.UNSUPPORTED_FILE_TYPE
        assert ".docx" in result.errors[0].message

    def test_rejects_file_exceeding_the_document_size_limit(self):
        settings = _settings(max_attachment_document_bytes=10)
        result = validate_upload("big.txt", "text/plain", b"0" * 20, settings)
        assert not result.valid
        assert result.errors[0].code == AttachmentErrorCode.FILE_TOO_LARGE

    def test_rejects_file_exceeding_the_image_size_limit_separately_from_documents(self):
        settings = _settings(max_attachment_image_bytes=10, max_attachment_document_bytes=10_000)
        result = validate_upload("cat.png", "image/png", _PNG_BYTES, settings)
        assert not result.valid
        assert result.errors[0].code == AttachmentErrorCode.FILE_TOO_LARGE

    def test_rejects_exceeding_total_combined_size(self):
        settings = _settings(max_total_attachment_bytes=50)
        result = validate_upload("a.txt", "text/plain", b"0" * 40, settings, current_total_bytes=20)
        assert not result.valid
        assert result.errors[0].code == AttachmentErrorCode.TOTAL_SIZE_TOO_LARGE

    def test_rejects_exceeding_max_attachment_count(self):
        settings = _settings(max_attachments_per_message=2)
        result = validate_upload(
            "a.txt", "text/plain", b"hello", settings, current_attachment_count=2
        )
        assert not result.valid
        assert result.errors[0].code == AttachmentErrorCode.TOO_MANY_ATTACHMENTS

    def test_rejects_content_that_does_not_match_its_extension(self):
        # A .png extension but the bytes don't start with the PNG signature.
        result = validate_upload("fake.png", "image/png", b"not a real png at all", _settings())
        assert not result.valid
        assert result.errors[0].code == AttachmentErrorCode.CORRUPTED_IMAGE

    def test_rejects_duplicate_within_the_same_request(self):
        file_hash = compute_sha256(_PNG_BYTES)
        result = validate_upload(
            "cat2.png",
            "image/png",
            _PNG_BYTES,
            _settings(),
            hashes_already_in_this_request=frozenset({file_hash}),
        )
        assert not result.valid
        assert result.errors[0].code == AttachmentErrorCode.DUPLICATE_FILE

    def test_warns_but_does_not_reject_on_mime_extension_mismatch(self):
        result = validate_upload("cat.png", "image/jpeg", _PNG_BYTES, _settings())
        assert result.valid
        assert result.warnings  # a warning was recorded, not a hard failure

    def test_accepts_pdf_by_signature(self):
        result = validate_upload(
            "doc.pdf", "application/pdf", b"%PDF-1.4\n" + b"0" * 50, _settings()
        )
        assert result.valid

    def test_accepts_plain_text_with_no_signature_check(self):
        result = validate_upload("notes.txt", "text/plain", b"hello world", _settings())
        assert result.valid
