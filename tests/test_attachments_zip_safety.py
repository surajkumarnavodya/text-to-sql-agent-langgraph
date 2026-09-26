"""Unit tests for attachments/zip_safety.py -- the decompression-bomb/
entry-count/path-safety preflight for DOCX/XLSX/PPTX (all plain ZIP
archives). Every fixture here is a real, freshly-built ZIP archive
constructed in-memory -- never a checked-in binary sample, per this
feature's own "avoid putting real malicious samples into the repository"
requirement. A "zip bomb" is simulated honestly: real files that compress
extremely well (repeated bytes), not a hand-faked ZipInfo.file_size, so the
central-directory metadata this module reads is exactly what a real
decompression-bomb archive would report.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from attachments.zip_safety import ZipSafetyError, check_zip_safety


def _make_zip(entries: dict[str, bytes], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class TestCheckZipSafety:
    def test_an_ordinary_small_archive_passes(self):
        zip_bytes = _make_zip({"word/document.xml": b"<xml>hello</xml>"})
        check_zip_safety(zip_bytes, max_uncompressed_bytes=10_000, max_entries=100)  # no raise

    def test_rejects_a_real_decompression_bomb_by_uncompressed_size(self):
        # Highly compressible content (all zeros) -- a small compressed
        # archive that would decompress to far more than the limit. Real
        # compression, not a faked ZipInfo, so this is a genuine test of
        # what the archive's own central directory reports.
        huge_repetitive_content = b"\x00" * (5 * 1024 * 1024)  # 5 MB of zeros
        zip_bytes = _make_zip({"bomb.bin": huge_repetitive_content})
        assert len(zip_bytes) < 50_000  # compresses down dramatically

        with pytest.raises(ZipSafetyError, match="decompression bomb"):
            check_zip_safety(zip_bytes, max_uncompressed_bytes=1_000_000, max_entries=100)

    def test_rejects_too_many_entries(self):
        entries = {f"file_{i}.txt": b"x" for i in range(50)}
        zip_bytes = _make_zip(entries)

        with pytest.raises(ZipSafetyError, match="entries"):
            check_zip_safety(zip_bytes, max_uncompressed_bytes=10_000_000, max_entries=10)

    def test_rejects_an_absolute_internal_path(self):
        zip_bytes = _make_zip({"/etc/passwd": b"x"})
        with pytest.raises(ZipSafetyError, match="unsafe internal path"):
            check_zip_safety(zip_bytes, max_uncompressed_bytes=10_000_000, max_entries=100)

    def test_rejects_a_path_traversal_internal_path(self):
        zip_bytes = _make_zip({"../../evil.txt": b"x"})
        with pytest.raises(ZipSafetyError, match="unsafe internal path"):
            check_zip_safety(zip_bytes, max_uncompressed_bytes=10_000_000, max_entries=100)

    def test_rejects_a_non_zip_file(self):
        with pytest.raises(ZipSafetyError, match="not a valid ZIP"):
            check_zip_safety(
                b"not a zip file at all", max_uncompressed_bytes=10_000, max_entries=10
            )

    def test_accepts_a_realistic_multi_entry_docx_shaped_archive(self):
        """A real DOCX is itself a multi-entry ZIP (content types, rels,
        document.xml, styles.xml, ...) -- confirms the limits don't
        false-positive on an ordinary, legitimately-structured document."""
        zip_bytes = _make_zip(
            {
                "[Content_Types].xml": b"<Types/>",
                "_rels/.rels": b"<Relationships/>",
                "word/document.xml": b"<document>Hello world</document>",
                "word/styles.xml": b"<styles/>",
            }
        )
        check_zip_safety(zip_bytes, max_uncompressed_bytes=1_000_000, max_entries=100)
