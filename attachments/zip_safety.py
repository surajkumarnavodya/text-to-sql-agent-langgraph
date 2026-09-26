"""Decompression-bomb / entry-count guard for ZIP-container document
formats -- DOCX/XLSX/PPTX are all plain ZIP archives under the hood (OOXML),
and none of `python-docx`/`openpyxl`/`python-pptx` impose any bound on total
decompressed size or entry count before parsing. A small, maliciously
crafted archive can therefore expand to gigabytes purely by being opened --
independent of whatever `Settings.max_attachment_document_bytes` already
caps on the *compressed* upload size, since that check only ever looks at
the bytes as received, never at what they'd decompress to.

Checked here using only the archive's own central-directory metadata
(`zipfile.ZipInfo.file_size`, which `zipfile` reads without decompressing
anything) -- before the real parser (`python-docx`/`openpyxl`/`python-pptx`)
ever touches a single entry, mirroring `security.malware_scanner`'s own
"check raw bytes before any parser touches them" ordering.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath


class ZipSafetyError(Exception):
    """Raised when a ZIP-container document fails the decompression-bomb/
    entry-count/path-safety preflight. Callers (the DOCX/XLSX/PPTX
    processors) catch this and convert it into an ordinary
    `ProcessingOutcome(status="failed", error=...)`, never let it
    propagate -- the same "never crash, always a structured failure"
    contract every other processor already has for a corrupt file.
    """


def check_zip_safety(
    file_bytes: bytes,
    *,
    max_uncompressed_bytes: int,
    max_entries: int,
) -> None:
    """Raises `ZipSafetyError` if `file_bytes` would decompress to more than
    `max_uncompressed_bytes` total, contains more than `max_entries`
    entries, or contains an entry with an unsafe path (absolute, or
    containing a `..` traversal segment).

    The path check is defense in depth, not the primary concern here: this
    app never extracts a ZIP entry to disk (`python-docx`/`openpyxl`/
    `python-pptx` all read entries into memory), so there's no real
    zip-slip write target today -- but an entry name built to *look* like a
    traversal attempt is itself a strong signal the file wasn't produced by
    an ordinary Office application, worth rejecting on its own.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise ZipSafetyError(f"This file is not a valid ZIP-based document: {exc}") from exc

    if len(infos) > max_entries:
        raise ZipSafetyError(
            f"This document contains {len(infos)} internal entries, exceeding the "
            f"{max_entries}-entry limit -- rejected as a possible decompression bomb."
        )

    total_uncompressed = sum(info.file_size for info in infos)
    if total_uncompressed > max_uncompressed_bytes:
        raise ZipSafetyError(
            f"This document would decompress to {total_uncompressed:,} bytes, exceeding "
            f"the {max_uncompressed_bytes:,}-byte limit -- rejected as a possible "
            "decompression bomb."
        )

    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ":" in name or ".." in PurePosixPath(name).parts:
            raise ZipSafetyError(f"This document contains an unsafe internal path: {name!r}.")


__all__ = ["ZipSafetyError", "check_zip_safety"]
