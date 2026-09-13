"""Dataset and result schema for the media-search benchmark -- see this
package's own `__init__.py` for why this is a parallel schema, not a reuse
of `eval/schema.py::BenchmarkCase`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MediaType = Literal["image", "video", "any"]


@dataclass(frozen=True)
class MediaBenchmarkCase:
    """One known (query -> expected asset) pair.

    `expected_filename` is matched against a hit's underlying source
    file's basename (`Path(hit_source_path).name`), not a `media_id` --
    a `media_id` is a content hash computed at ingestion time and isn't
    known ahead of time / is meaningless to hand-author into a dataset
    file. For a video case, `expected_timestamp` (seconds into the
    source video) is optional -- when given, a hit is only correct if its
    segment's `[timestamp_start, timestamp_end)` range contains it.
    """

    id: str
    query: str
    expected_filename: str
    media_type: MediaType = "any"
    expected_timestamp: float | None = None
    notes: str = ""


@dataclass
class MediaCaseResult:
    """What running one case against the live index actually produced."""

    case_id: str
    query: str
    expected_filename: str
    hit_filenames: list[str] = field(default_factory=list)
    top_hit_correct: bool = False
    hit_at_k: bool = False
    timestamp_correct: bool | None = None  # None when not applicable (no expected_timestamp)
    error: str | None = None


@dataclass
class MediaBenchmarkReport:
    """Aggregated output of a full media-search benchmark run."""

    run_id: str
    timestamp: str
    total_cases: int
    hit_at_1: float
    hit_at_k: float
    timestamp_accuracy: float | None
    results: list[MediaCaseResult]

    @property
    def failures(self) -> list[MediaCaseResult]:
        return [r for r in self.results if not r.hit_at_k]
