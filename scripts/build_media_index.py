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

Every file now also passes through the mandatory pre-ingestion moderation
gate (`moderation/gate.py`) as part of `ingest_file` -- a rejected file is
reported separately from a merely-skipped (already-indexed) one in this
script's summary, and never counted as a failure (rejection is an expected,
working outcome of the gate, not an error in this tool).

Requires `ENABLE_MEDIA_SEARCH=true`, a real `MEDIA_LIBRARY_PATH`, and the
moderation provider/metadata store (`AZURE_CONTENT_SAFETY_*`,
`MODERATION_STORE_CONNECTION_STRING`) set in `.env` first -- checked once,
upfront, before walking any files, so a missing moderation configuration
fails fast with one clear message instead of the same error repeated once
per file.

Files are ingested concurrently (`Settings.media_ingest_workers` worker
threads, default 4) via `concurrent.futures.ThreadPoolExecutor` -- this
repo's first use of a bounded worker pool, appropriate here because each
file's ingestion is dominated by I/O-bound work (the moderation provider's
API call, database writes) that benefits from threads despite the GIL. Set
`MEDIA_INGEST_WORKERS=1` to fall back to fully sequential processing.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings, configure_logging, get_settings  # noqa: E402
from media.exceptions import MediaFileTooLargeError, UnsupportedMediaTypeError  # noqa: E402
from media.ingest import IngestResult, ingest_file  # noqa: E402
from moderation.exceptions import ModerationNotConfiguredError  # noqa: E402
from moderation.store import ensure_schema, get_moderation_engine  # noqa: E402

logger = logging.getLogger(__name__)

Outcome = Literal["indexed", "skipped", "rejected", "failed"]


def _process_one(path: Path, settings: Settings, force: bool) -> tuple[Outcome, IngestResult | None, Exception | None]:
    """Ingests one file and classifies the outcome -- run inside a worker
    thread (`main`'s `ThreadPoolExecutor`), so this must not mutate any
    shared state; the caller aggregates counts from the returned tuple."""
    try:
        result = ingest_file(path, settings, force=force)
    except (UnsupportedMediaTypeError, MediaFileTooLargeError) as exc:
        return "skipped", None, exc
    except Exception as exc:  # noqa: BLE001 - one bad file must not abort the whole run
        return "failed", None, exc

    if result.moderation_status == "rejected":
        return "rejected", result, None
    if result.media_type == "video" and result.segments_indexed == 0:
        return "skipped", result, None
    return "indexed", result, None


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

    # Gracefully handle missing media library: create it if it doesn't exist
    media_path = settings.media_library_path
    if not media_path.is_dir():
        logger.info("MEDIA_LIBRARY_PATH (%s) does not exist -- creating it.", media_path)
        try:
            media_path.mkdir(parents=True, exist_ok=True)
            logger.info("Created media library directory at %s. Add media files (images/videos) and re-run this script.", media_path)
            return
        except OSError as exc:
            logger.error("Failed to create MEDIA_LIBRARY_PATH (%s): %s", media_path, exc)
            sys.exit(1)

    # Pre-flight: fail fast with one clear message rather than the same
    # ModerationNotConfiguredError repeated once per file once the loop
    # below starts.
    try:
        moderation_engine = get_moderation_engine(settings)
        ensure_schema(moderation_engine)
    except ModerationNotConfiguredError as exc:
        logger.error(str(exc))
        sys.exit(1)

    files = [p for p in settings.media_library_path.rglob("*") if p.is_file()]
    if not files:
        logger.warning("No files found under %s.", settings.media_library_path)
        return

    indexed = skipped = failed = rejected = 0
    segments_total = 0
    started_at = time.perf_counter()

    with ThreadPoolExecutor(max_workers=settings.media_ingest_workers) as pool:
        future_to_path = {pool.submit(_process_one, path, settings, args.force): path for path in files}
        for future in as_completed(future_to_path):
            path = future_to_path[future]
            outcome, result, exc = future.result()

            if outcome == "skipped":
                if exc is not None:
                    logger.debug("[%s] skipped: %s", path, exc)
                else:
                    logger.info("[%s] unchanged, skipped", path)
                skipped += 1
            elif outcome == "rejected":
                logger.warning("[%s] rejected by content moderation", path)
                rejected += 1
            elif outcome == "failed":
                logger.warning("[%s] ingestion failed: %s", path, exc, exc_info=exc)
                failed += 1
            else:  # "indexed"
                assert result is not None
                indexed += 1
                segments_total += result.segments_indexed if result.media_type == "video" else 0
                logger.info(
                    "[%s] indexed as %s%s",
                    path,
                    result.media_type,
                    f" ({result.segments_indexed} segment(s))" if result.media_type == "video" else "",
                )

    total_seconds = time.perf_counter() - started_at
    logger.info(
        "Done in %.1fs: %d indexed (%d video segment(s) total), %d skipped, %d rejected, %d failed.",
        total_seconds,
        indexed,
        segments_total,
        skipped,
        rejected,
        failed,
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
