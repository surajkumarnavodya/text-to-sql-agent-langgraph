"""Secure on-disk storage for chat attachment bytes.

Two separate defenses against a hostile filename, not one:

1. The actual on-disk path is built entirely from `attachment_id` (a
   server-generated id, never derived from user input at all -- see
   `attachments.models._new_attachment_id`) plus the *validated* extension
   (`attachments.validation.EXTENSION_TO_MEDIA_TYPE`'s own keys) -- the
   original filename never touches a filesystem path, which is the
   strongest possible path-traversal defense: there is nothing to escape
   with `../` or a null byte if the filename was never used to build the
   path in the first place.
2. `sanitize_filename` produces a *display-only* safe version of the
   original name (for `Attachment.safe_filename`, shown in the UI/logs) --
   belt-and-suspenders, since (1) alone already makes the original filename
   security-irrelevant to storage, but a sanitized display name is still
   worth having so a malicious-looking filename never renders raw in the UI
   either.

Storage lives under `Settings.attachment_storage_dir` -- outside any
directory this app serves as static content, the same "outside the public
web root" rule `Settings.chroma_persist_dir`/`voice/models/` already follow.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from config.settings import Settings

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]")
_MAX_DISPLAY_NAME_LENGTH = 150


def sanitize_filename(filename: str) -> str:
    """Returns a display-safe version of `filename` -- strips any directory
    component (path traversal), drops characters outside a small allowlist,
    collapses whitespace, and caps length. Never returns an empty string --
    falls back to `"attachment"` (plus the original extension, if any) so a
    filename that sanitizes to nothing (e.g. one made entirely of path
    separators) still displays as something sensible."""
    # Path(...).name strips any directory component regardless of OS
    # separator style (a filename containing "/" or "\" from a client that
    # didn't itself strip it) -- this is what actually neutralizes
    # "../../etc/passwd"-shaped input; everything below is cosmetic on top.
    base = Path(filename.replace("\\", "/")).name
    extension = Path(base).suffix
    stem = base[: len(base) - len(extension)] if extension else base

    cleaned_stem = _UNSAFE_CHARS.sub("_", stem).strip(" ._") or "attachment"
    cleaned_extension = _UNSAFE_CHARS.sub("", extension)

    result = f"{cleaned_stem}{cleaned_extension}"
    if len(result) > _MAX_DISPLAY_NAME_LENGTH:
        keep = _MAX_DISPLAY_NAME_LENGTH - len(cleaned_extension)
        result = f"{cleaned_stem[:keep]}{cleaned_extension}"
    return result


def attachment_file_path(settings: Settings, attachment_id: str, extension: str) -> Path:
    """Builds the on-disk path for one attachment's bytes -- entirely from
    `attachment_id` and the (already-validated) extension, never from the
    caller-supplied filename. See this module's docstring for why that
    matters."""
    safe_extension = _UNSAFE_CHARS.sub("", extension) or ".bin"
    return settings.attachment_storage_dir / f"{attachment_id}{safe_extension}"


def save_attachment_bytes(
    settings: Settings, attachment_id: str, extension: str, file_bytes: bytes
) -> Path:
    """Writes `file_bytes` to this attachment's on-disk location, creating
    the storage directory if needed. Returns the path actually written.

    Raises:
        OSError: on a genuine disk failure (permissions, out of space) --
            deliberately not swallowed here; `attachments.pipeline` is what
            translates this into a structured `AttachmentError`
            (`STORAGE_FAILED`) for the API/UI to show.
    """
    settings.attachment_storage_dir.mkdir(parents=True, exist_ok=True)
    path = attachment_file_path(settings, attachment_id, extension)
    path.write_bytes(file_bytes)
    return path


def delete_attachment_file(path: str | Path) -> None:
    """Deletes one attachment's on-disk bytes, if present. Never raises --
    a missing file (already deleted, or never written) is a no-op, matching
    every other best-effort cleanup helper in this codebase
    (`media_gen/download.py`'s temp-file cleanup, `rag/ingestion.py`'s
    embedded-image temp files)."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.warning("[attachments] failed to delete attachment file %s", path, exc_info=True)


def purge_expired_attachments(settings: Settings, *, now: float | None = None) -> int:
    """Deletes every on-disk attachment file older than
    `Settings.attachment_retention_hours`. Called opportunistically (from
    `attachments.store.AttachmentStore.put`, on each new upload) rather than
    on a timer -- see `Settings.attachment_retention_hours`'s own docstring
    for why this app has no background scheduler.

    Only touches files directly under `attachment_storage_dir` (no
    recursion) -- every attachment file lives flat in that one directory
    (see `attachment_file_path`), so there's nothing else to walk. Returns
    the number of files deleted, for logging/tests.
    """
    directory = settings.attachment_storage_dir
    if not directory.exists():
        return 0

    cutoff = (now if now is not None else time.time()) - (
        settings.attachment_retention_hours * 3600
    )
    deleted = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                deleted += 1
        except OSError:
            logger.warning("[attachments] failed to purge %s", path, exc_info=True)
    if deleted:
        logger.info("[attachments] purged %d expired attachment file(s)", deleted)
    return deleted
