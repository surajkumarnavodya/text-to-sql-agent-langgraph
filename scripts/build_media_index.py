"""Standalone entry point: walk `MEDIA_LIBRARY_PATH` and (re)build the
media-search index (images + video segments).

Usage (from repo root, with the venv activated):

    python scripts\\build_media_index.py           # skip files already indexed
    python scripts\\build_media_index.py --force    # re-index every file

Mirrors `scripts/build_embeddings.py`'s shape (a batch CLI over
`media.ingest.ingest_file`, the same "skip if unchanged unless --force"
behavior). A single bad/unsupported/oversized file is logged and skipped,
never aborting the whole run -- a media folder will realistically contain
some non-media files (`.DS_Store`, thumbnails, sidecar files) that aren't
errors, just not this tool's concern.

Requires `ENABLE_MEDIA_SEARCH=true` and a real `MEDIA_LIBRARY_PATH` set in
`.env` first.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import configure_logging, get_settings  # noqa: E402
from media.exceptions import MediaFileTooLargeError, UnsupportedMediaTypeError  # noqa: E402
from media.ingest import ingest_file  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-index every file, even one already indexed by content hash.",
    )
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()

    if not settings.enable_media_search:
        logger.error("ENABLE_MEDIA_SEARCH is not set -- nothing to build.")
        sys.exit(1)
    if not settings.media_library_path:
        logger.error("MEDIA_LIBRARY_PATH is not set -- nothing to walk.")
        sys.exit(1)
    if not settings.media_library_path.is_dir():
        logger.error("MEDIA_LIBRARY_PATH (%s) is not a directory.", settings.media_library_path)
        sys.exit(1)

    files = [p for p in settings.media_library_path.rglob("*") if p.is_file()]
    if not files:
        logger.warning("No files found under %s.", settings.media_library_path)
        return

    indexed = skipped = failed = 0
    segments_total = 0
    started_at = time.perf_counter()
    for path in files:
        file_started_at = time.perf_counter()
        try:
            result = ingest_file(path, settings, force=args.force)
        except (UnsupportedMediaTypeError, MediaFileTooLargeError) as exc:
            logger.debug("[%s] skipped: %s", path, exc)
            skipped += 1
            continue
        except Exception:  # noqa: BLE001 - one bad file must not abort the whole run
            logger.warning("[%s] ingestion failed", path, exc_info=True)
            failed += 1
            continue

        duration_ms = (time.perf_counter() - file_started_at) * 1000
        if result.media_type == "video" and result.segments_indexed == 0:
            logger.info("[%s] unchanged, skipped (%.0fms)", path, duration_ms)
            skipped += 1
            continue
        indexed += 1
        segments_total += result.segments_indexed
        logger.info(
            "[%s] indexed as %s%s (%.0fms)",
            path,
            result.media_type,
            f" ({result.segments_indexed} segment(s))" if result.media_type == "video" else "",
            duration_ms,
        )

    total_seconds = time.perf_counter() - started_at
    logger.info(
        "Done in %.1fs: %d indexed (%d video segment(s) total), %d skipped, %d failed.",
        total_seconds,
        indexed,
        segments_total,
        skipped,
        failed,
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
