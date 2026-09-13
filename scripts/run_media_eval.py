"""Standalone entry point: runs the media-search benchmark
(`eval/media_benchmark/dataset.yaml`) against the real, indexed media
library and prints a report.

Requires `ENABLE_MEDIA_SEARCH=true`, a real `MEDIA_LIBRARY_PATH`, and
`python scripts\\build_media_index.py` already run -- like
`scripts/run_benchmark.py`, this is manual/real-library-required, not
part of the `pytest` suite, never run by CI.

This repo has no checked-in media library or dataset to grade against
(media content isn't the kind of thing that belongs in git the way sample
SQL data conceptually could be) -- `eval/media_benchmark/dataset.yaml`
ships as an empty template; see that file's own comments for how to
populate it against your own small labeled folder.

Usage (from repo root, with the venv activated):

    python scripts\\run_media_eval.py
    python scripts\\run_media_eval.py --top-k 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import configure_logging, get_settings  # noqa: E402
from eval.media_benchmark.dataset_loader import load_media_dataset  # noqa: E402
from eval.media_benchmark.reporting import render_report  # noqa: E402
from eval.media_benchmark.runner import run_media_benchmark  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=5, help="Hits requested per query.")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()

    if not settings.enable_media_search or not settings.media_library_path:
        print("ENABLE_MEDIA_SEARCH and MEDIA_LIBRARY_PATH must both be set -- nothing to grade.")
        sys.exit(1)

    cases = load_media_dataset()
    if not cases:
        print(
            "eval/media_benchmark/dataset.yaml has no cases yet -- see that file's "
            "own comments for how to populate it against your own indexed library."
        )
        return

    report = run_media_benchmark(cases, settings, top_k=args.top_k)
    print(render_report(report))
    if report.hit_at_k < 1.0:
        sys.exit(1)


if __name__ == "__main__":
    main()
