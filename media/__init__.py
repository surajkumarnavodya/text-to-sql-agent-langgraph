"""Multimodal media search -- content-based search over an untagged local
image/video library (no filenames, no manual tags).

Standalone library module, not imported by the core SQL pipeline
(`agent/graph.py`) at all -- only `agent/orchestrator/nodes.py
::media_search_node` depends on it, and only when
`Settings.enable_media_search` is on. Mirrors `media_gen/`'s shape (a
tested wrapper around an external capability, kept out of `agent/`) and
`rag/`'s shape (ingest -> embed -> store -> retrieve -> answer-with-
citations), applied to local, untagged binary media instead of uploaded
PDFs or a generation API.

See `CLAUDE.md`'s "Media search" section for the full design (why local
CLIP embeddings by default, why ChromaDB, why scene-change keyframing,
why captioning/OCR/ASR all fail open).
"""

from __future__ import annotations

from .exceptions import (
    MediaEmbeddingModelNotFoundError,
    MediaFileTooLargeError,
    MediaSearchError,
    MediaSearchNotConfiguredError,
    UnsupportedMediaTypeError,
)
from .ingest import IngestResult, ingest_file
from .search import MediaHit, search_media

__all__ = [
    "IngestResult",
    "MediaEmbeddingModelNotFoundError",
    "MediaFileTooLargeError",
    "MediaHit",
    "MediaSearchError",
    "MediaSearchNotConfiguredError",
    "UnsupportedMediaTypeError",
    "ingest_file",
    "search_media",
]
