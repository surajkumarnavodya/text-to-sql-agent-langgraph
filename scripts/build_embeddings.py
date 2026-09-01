"""Standalone entry point: introspect the real database and (re)build the ChromaDB schema index.

Usage (from repo root, with the venv activated):

    python scripts\\build_embeddings.py           # skip if schema hash unchanged
    python scripts\\build_embeddings.py --force    # re-embed regardless

Requires a working database connection (see .env / README) -- run
`python scripts\\test_db_connection.py` first if this fails unexpectedly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import ConfigurationError, configure_logging, get_settings  # noqa: E402
from db.connection import get_read_only_engine, test_connection  # noqa: E402
from embeddings.schema_indexer import refresh_schema_index  # noqa: E402

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

    try:
        engine = get_read_only_engine(settings)
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    check = test_connection(settings)
    if not check.success:
        logger.error("Cannot build embeddings -- database connection failed: %s", check.message)
        sys.exit(1)

    sampled_tables = refresh_schema_index(engine, settings=settings, force=args.force)
    logger.info("Schema index ready: %d table(s) available for retrieval.", len(sampled_tables))


if __name__ == "__main__":
    main()
