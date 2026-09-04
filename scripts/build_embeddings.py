"""Standalone entry point: introspect the real database(s) and (re)build the ChromaDB schema index.

Usage (from repo root, with the venv activated):

    python scripts\\build_embeddings.py           # skip if schema hash unchanged
    python scripts\\build_embeddings.py --force    # re-embed regardless

Builds one Chroma collection per configured database (`Settings.databases`
-- a plain single-database `.env` has exactly one, named "default"; a
multi-database `.env` sets `DB_CONNECTIONS=name1,name2,...`). One
unreachable/misconfigured connection does not stop the others from being
indexed -- see `embeddings.schema_indexer.refresh_all_schema_indexes`.

Requires at least one working database connection (see .env / README) --
run `python scripts\\test_db_connection.py` first if this fails unexpectedly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import configure_logging, get_settings  # noqa: E402
from db.connection import test_connection  # noqa: E402
from embeddings.schema_indexer import refresh_all_schema_indexes  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed even if the introspected schema hasn't changed since the last build.",
    )
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()

    reachable = []
    for config in settings.databases:
        check = test_connection(config)
        if check.success:
            reachable.append(config.name)
        else:
            logger.warning(
                "Database %r is unreachable, will be skipped: %s", config.name, check.message
            )

    if not reachable:
        logger.error("Cannot build embeddings -- no configured database is reachable.")
        sys.exit(1)

    results = refresh_all_schema_indexes(settings, force=args.force)
    if not results:
        logger.error("No database's schema index could be built.")
        sys.exit(1)

    for db_name, sampled_tables in results.items():
        logger.info(
            "[%s] Schema index ready: %d table(s) available for retrieval.",
            db_name,
            len(sampled_tables),
        )

    skipped = sorted(set(config.name for config in settings.databases) - set(results))
    if skipped:
        logger.warning("Skipped (index not built): %s", ", ".join(skipped))


if __name__ == "__main__":
    main()
