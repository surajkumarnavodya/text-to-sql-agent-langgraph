"""Unit tests for attachments/storage.py."""

from __future__ import annotations

from attachments.storage import (
    attachment_file_path,
    delete_attachment_file,
    purge_expired_attachments,
    sanitize_filename,
    save_attachment_bytes,
)
from config.settings import Settings


class TestSanitizeFilename:
    def test_strips_directory_components(self):
        assert sanitize_filename("../../etc/passwd.png") == "passwd.png"

    def test_strips_windows_style_path_separators(self):
        assert sanitize_filename("C:\\Users\\evil\\..\\..\\file.txt") == "file.txt"

    def test_removes_unsafe_characters(self):
        result = sanitize_filename('weird<>:"|?*name.txt')
        assert "<" not in result
        assert result.endswith(".txt")

    def test_never_returns_empty(self):
        result = sanitize_filename("../../")
        assert result != ""

    def test_caps_length(self):
        long_name = "a" * 500 + ".txt"
        result = sanitize_filename(long_name)
        assert len(result) <= 150


class TestAttachmentFilePath:
    def test_path_is_built_only_from_id_and_extension(self, tmp_path):
        settings = Settings(_env_file=None, attachment_storage_dir=tmp_path)
        path = attachment_file_path(settings, "att_abc123", ".png")
        assert path == tmp_path / "att_abc123.png"

    def test_path_traversal_in_extension_is_neutralized(self, tmp_path):
        settings = Settings(_env_file=None, attachment_storage_dir=tmp_path)
        path = attachment_file_path(settings, "att_abc123", "../../evil")
        # Unsafe characters (including "/") are stripped from the extension --
        # the result must still resolve inside the storage directory.
        assert path.parent == tmp_path


class TestSaveAndDeleteAttachmentBytes:
    def test_round_trip(self, tmp_path):
        settings = Settings(_env_file=None, attachment_storage_dir=tmp_path)
        path = save_attachment_bytes(settings, "att_1", ".txt", b"hello world")
        assert path.read_bytes() == b"hello world"

        delete_attachment_file(path)
        assert not path.exists()

    def test_delete_missing_file_does_not_raise(self, tmp_path):
        delete_attachment_file(tmp_path / "does_not_exist.txt")


class TestPurgeExpiredAttachments:
    def test_deletes_files_older_than_retention_window(self, tmp_path):
        settings = Settings(
            _env_file=None, attachment_storage_dir=tmp_path, attachment_retention_hours=1
        )
        old_file = tmp_path / "old.txt"
        old_file.write_text("old")
        new_file = tmp_path / "new.txt"
        new_file.write_text("new")

        import os
        import time

        old_time = time.time() - (2 * 3600)
        os.utime(old_file, (old_time, old_time))

        deleted_count = purge_expired_attachments(settings)

        assert deleted_count == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_missing_directory_is_a_no_op(self, tmp_path):
        settings = Settings(_env_file=None, attachment_storage_dir=tmp_path / "does_not_exist_yet")
        assert purge_expired_attachments(settings) == 0
