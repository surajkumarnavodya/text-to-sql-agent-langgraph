"""Loads the media-search benchmark dataset from `eval/media_benchmark/dataset.yaml`.

Pure and dependency-free (no live library, no Ollama) -- safe to unit test
directly and safe to call from `eval.media_benchmark.runner` without any
live-infrastructure precondition, mirroring `eval.dataset_loader`'s own
design.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from eval.media_benchmark.schema import MediaBenchmarkCase

_DEFAULT_PATH = Path(__file__).resolve().parent / "dataset.yaml"

_REQUIRED_FIELDS = ("id", "query", "expected_filename")


class MediaDatasetValidationError(ValueError):
    """Raised when `dataset.yaml` is malformed in a way that would silently
    produce a meaningless benchmark run."""


def load_media_dataset(path: Path | None = None) -> tuple[MediaBenchmarkCase, ...]:
    """Loads and validates every case in `dataset.yaml`.

    Returns an empty tuple (not an error) for a missing or empty file --
    this is the expected state for a fresh clone that hasn't populated its
    own dataset yet; `scripts/run_media_eval.py` reports that plainly
    rather than treating it as a crash.
    """
    resolved_path = path or _DEFAULT_PATH
    if not resolved_path.exists():
        return ()

    raw = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
    raw_cases = raw.get("cases") or []

    cases: list[MediaBenchmarkCase] = []
    seen_ids: set[str] = set()
    for entry in raw_cases:
        missing = [f for f in _REQUIRED_FIELDS if f not in entry]
        if missing:
            raise MediaDatasetValidationError(
                f"Case {entry!r} is missing required field(s): {missing}"
            )
        if entry["id"] in seen_ids:
            raise MediaDatasetValidationError(f"Duplicate case id: {entry['id']!r}")
        seen_ids.add(entry["id"])
        cases.append(
            MediaBenchmarkCase(
                id=entry["id"],
                query=entry["query"],
                expected_filename=entry["expected_filename"],
                media_type=entry.get("media_type", "any"),
                expected_timestamp=entry.get("expected_timestamp"),
                notes=entry.get("notes", ""),
            )
        )
    return tuple(cases)
