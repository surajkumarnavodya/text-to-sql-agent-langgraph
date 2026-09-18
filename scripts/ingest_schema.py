"""Standalone entry point: idempotent business-context ingestion for one database.

Usage (from repo root, with the venv activated):

    python -m scripts.ingest_schema --database-id default
    python -m scripts.ingest_schema --database-id default --dry-run

Discovers live schema (tables/columns/FKs, via the same read-only
introspection `scripts/build_embeddings.py` uses) plus
`data/knowledge/{glossary,metrics,sql_examples}.yaml` and
`data/knowledge/documentation/*`, turns them into typed chunks
(`retrieval/chunking.py`), and upserts only what changed into this
database's business-context Chroma collection (`retrieval/ingestion.py`) --
safe to run repeatedly (a cron job, a CI step, after editing a knowledge
file) since unchanged chunks are skipped, not re-embedded or duplicated.

This is a *separate* index from the one `scripts/build_embeddings.py`
builds -- that one is the schema-DDL collection `embeddings/schema_indexer.py`
already scoped SQL generation with (unaffected by this script); this one is
the newer business-context layer (`retrieval/`, see
`docs/vector-retrieval-design.md`). Run both after a schema/knowledge change
that should reach the agent's retrieval.

`--dry-run` computes and prints the same summary but makes no embedding
calls and no vector-store writes at all -- useful to preview what a real
run would do (e.g. after editing a knowledge YAML file) before spending any
embedding calls.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retrieval.ingestion import IngestionSummary, run_ingestion  # noqa: E402

from config.settings import configure_logging, get_settings  # noqa: E402
from db.connection import test_connection  # noqa: E402

logger = logging.getLogger(__name__)


def _print_summary(summary: IngestionSummary) -> None:
    label = "DRY RUN" if summary.dry_run else "INGESTION"
    print(f"\n[{label}] database={summary.database_id!r}")
    print(f"  discovered records : {summary.discovered_records}")
    print(f"  generated chunks   : {summary.generated_chunks}")
    print(f"  inserted           : {summary.inserted}")
    print(f"  updated            : {summary.updated}")
    print(f"  skipped (unchanged): {summary.skipped}")
    print(f"  deleted (stale)    : {summary.deleted}")
    print(f"  failures           : {summary.failures}")
    print(f"  duration           : {summary.duration_seconds:.2f}s")
    if summary.counts_by_type:
        by_type = ", ".join(f"{k}={v}" for k, v in sorted(summary.counts_by_type.items()))
        print(f"  chunks by type     : {by_type}")
    if summary.errors:
        print(f"  errors             : {summary.errors}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-id",
        required=True,
        help="Configured database name (Settings.databases[i].name; 'default' for a "
        "plain single-database .env).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print the ingestion summary without embedding or writing anything.",
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

    try:
        summary = run_ingestion(args.database_id, settings, dry_run=args.dry_run)
    except Exception:
        logger.exception("Ingestion failed for database %r", args.database_id)
        sys.exit(1)

    _print_summary(summary)
    if summary.failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
