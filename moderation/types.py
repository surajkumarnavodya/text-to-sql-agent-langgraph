"""Shared dataclasses for the moderation gate -- kept separate from
`moderation/gate.py`/`moderation/provider.py` so both can import these
without a circular import (the provider produces `CategoryResult`s from a
`ModerationChunk`; the gate aggregates them into a `ModerationDecision`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from moderation.taxonomy import Category

ChunkContentType = Literal["text", "image"]


@dataclass(frozen=True)
class ModerationChunk:
    """One unit of content to moderate -- a PDF page's text, an embedded
    PDF image, a whole (or tiled) ingested image, or one video segment's
    keyframe/caption text. `chunk_index` is only for the audit trail
    (log/report which chunk triggered a decision), not a storage key.
    """

    chunk_index: int
    content_type: ChunkContentType
    text: str | None = None
    image_path: Path | None = None


@dataclass(frozen=True)
class CategoryResult:
    """One category's outcome for one chunk, from either the provider or
    the text blocklist."""

    category: Category
    triggered: bool
    severity: int | None = None  # None for a blocklist-only match (no severity concept)
    source: Literal["provider", "blocklist", "not_checked"] = "provider"


@dataclass(frozen=True)
class ChunkModerationResult:
    """Every category's outcome for one chunk."""

    chunk_index: int
    categories: tuple[CategoryResult, ...]

    @property
    def hard_rejected_categories(self) -> tuple[Category, ...]:
        from moderation.taxonomy import decision_for

        return tuple(
            r.category for r in self.categories if r.triggered and decision_for(r.category) == "hard_reject"
        )

    @property
    def soft_flagged_categories(self) -> tuple[Category, ...]:
        from moderation.taxonomy import decision_for

        return tuple(
            r.category for r in self.categories if r.triggered and decision_for(r.category) == "soft_flag"
        )


@dataclass(frozen=True)
class ModerationDecision:
    """The asset-level outcome of moderating every chunk -- what
    `media/ingest.py`/`rag/ingestion.py` actually act on.
    """

    status: Literal["passed", "rejected"]
    triggering_categories: tuple[Category, ...] = field(default_factory=tuple)
    soft_flagged_categories: tuple[Category, ...] = field(default_factory=tuple)
    triggering_chunk_index: int | None = None
    chunk_results: tuple[ChunkModerationResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.status == "passed"
