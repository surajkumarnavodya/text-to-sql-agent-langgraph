"""Drives `media.search.search_media` for every case in the dataset and
grades each result -- combines what `eval/runner.py`/`eval/evaluators.py`/
`eval/metrics.py` are three separate modules for on the SQL side, since
the media-search grading logic is small enough not to need that split.

Grading is by **retrieved-asset identity**, not embedding similarity: a
hit is correct when its underlying source file's basename matches the
case's `expected_filename` (resolved via `media.store`'s metadata lookup,
since `media.search.MediaHit` deliberately never carries a raw path -- see
that module's own docstring) -- and, when `expected_timestamp` is given,
the matching hit's segment range must contain it.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

from config.settings import Settings
from eval.media_benchmark.schema import MediaBenchmarkCase, MediaBenchmarkReport, MediaCaseResult
from media.search import MediaHit, search_media
from media.store import get_image_metadata, get_segment_metadata


def _hit_filename(hit: MediaHit, settings: Settings) -> str | None:
    metadata = (
        get_segment_metadata(hit.media_id, settings)
        if hit.media_type == "video"
        else get_image_metadata(hit.media_id, settings)
    )
    if not metadata:
        return None
    return Path(str(metadata["source_path"])).name


def run_case(case: MediaBenchmarkCase, settings: Settings, top_k: int) -> MediaCaseResult:
    try:
        hits = search_media(case.query, settings, media_type=case.media_type, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 - one bad case must not abort the whole run
        return MediaCaseResult(
            case_id=case.id,
            query=case.query,
            expected_filename=case.expected_filename,
            error=str(exc),
        )

    resolved = [(hit, _hit_filename(hit, settings)) for hit in hits]
    hit_filenames = [name for _hit, name in resolved if name]

    top_hit_correct = bool(hit_filenames) and hit_filenames[0] == case.expected_filename
    hit_at_k = case.expected_filename in hit_filenames

    timestamp_correct: bool | None = None
    if case.expected_timestamp is not None:
        timestamp_correct = any(
            name == case.expected_filename
            and hit.timestamp_start is not None
            and hit.timestamp_end is not None
            and hit.timestamp_start <= case.expected_timestamp < hit.timestamp_end
            for hit, name in resolved
        )

    return MediaCaseResult(
        case_id=case.id,
        query=case.query,
        expected_filename=case.expected_filename,
        hit_filenames=hit_filenames,
        top_hit_correct=top_hit_correct,
        hit_at_k=hit_at_k,
        timestamp_correct=timestamp_correct,
    )


def run_media_benchmark(
    cases: tuple[MediaBenchmarkCase, ...], settings: Settings, top_k: int = 5
) -> MediaBenchmarkReport:
    """Runs every case and aggregates hit@1 / hit@k / timestamp accuracy."""
    results = [run_case(case, settings, top_k) for case in cases]
    total = len(results)

    def _rate(predicate) -> float:
        return sum(1 for r in results if predicate(r)) / total if total else 0.0

    timestamp_cases = [r for r in results if r.timestamp_correct is not None]
    timestamp_accuracy = (
        sum(1 for r in timestamp_cases if r.timestamp_correct) / len(timestamp_cases)
        if timestamp_cases
        else None
    )

    return MediaBenchmarkReport(
        run_id=uuid.uuid4().hex[:12],
        timestamp=datetime.datetime.now(datetime.UTC).isoformat(),
        total_cases=total,
        hit_at_1=_rate(lambda r: r.top_hit_correct),
        hit_at_k=_rate(lambda r: r.hit_at_k),
        timestamp_accuracy=timestamp_accuracy,
        results=results,
    )
