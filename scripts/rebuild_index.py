"""Standalone entry point: full, explicit rebuild of one database's
business-context vector index.

Usage (from repo root, with the venv activated):

    python -m scripts.rebuild_index --database-id default

Unlike `scripts/ingest_schema.py` (safe to run unattended/repeatedly --
only ever inserts/updates/deletes the specific chunks that actually
changed), this **drops the entire collection first** (`retrieval.ingestion
.rebuild_collection`) before re-ingesting everything from scratch -- every
chunk is reported as newly inserted, never updated/skipped. Reserved for
when the collection's own shape needs to change (e.g. after changing
`RETRIEVAL_SIMILARITY_METRIC`, which -- like `embeddings/schema_indexer.py`'s
own collection -- is fixed at collection-creation time in Chroma and can't
be altered in place) or when recovering from a corrupted/inconsistent
index. Prompts for confirmation unless `--yes` is passed, since this is a
destructive operation on this database's retrieval index (not on the
business database itself, which this script never touches).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retrieval.ingestion import rebuild_collection  # noqa: E402

from config.settings import configure_logging, get_settings  # noqa: E402
from db.connection import test_connection  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-id", required=True, help="Configured database name.")
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt (for non-interactive use)."
    )
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()

    matching = [c for c in settings.databases if c.name == args.database_id]
    if not matching:
        configured = ", ".join(c.name for c in settings.databases) or "(none configured)"
        logger.error(
            "Unknown --database-id %r. Configured databases: %s", args.database_id, configured
        )
        sys.exit(1)

    check = test_connection(matching[0])
    if not check.success:
        logger.error("Database %r is unreachable: %s", args.database_id, check.message)
        sys.exit(1)

    if not args.yes:
        answer = input(
            f"This will DELETE the entire business-context index for database "
            f"{args.database_id!r} and rebuild it from scratch. Continue? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    try:
        summary = rebuild_collection(args.database_id, settings)
    except Exception:
        logger.exception("Rebuild failed for database %r", args.database_id)
        sys.exit(1)

    print(f"\n[REBUILD] database={summary.database_id!r}")
    print(f"  inserted : {summary.inserted}")
    print(f"  failures : {summary.failures}")
    print(f"  duration : {summary.duration_seconds:.2f}s")
    if summary.errors:
        print(f"  errors   : {summary.errors}")
    if summary.failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
