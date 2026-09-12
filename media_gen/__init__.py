"""IMA Studio (imaclaw.ai) media generation -- image/video/audio.

Standalone library module, not imported by the core SQL pipeline
(`agent/graph.py`) at all -- only `agent/orchestrator/nodes.py::generation_node`
depends on it, and only when `Settings.enable_media_generation` is on.
Image generation is confirmed working end-to-end against a real IMA
account; video shares the same client/task-creation code path but hasn't
been separately confirmed with a live call yet -- see `media_gen/client.py`'s
module docstring for the full endpoint contract.
"""

from __future__ import annotations

from .audio import generate_audio
from .cache import MediaCache, MediaCacheEntry, get_media_cache
from .client import (
    IMAClient,
    MediaGenerationError,
    MediaGenerationNotConfiguredError,
    MediaResult,
    get_ima_client,
)
from .download import download_media_bytes
from .image import generate_image
from .video import generate_video

__all__ = [
    "IMAClient",
    "MediaCache",
    "MediaCacheEntry",
    "MediaGenerationError",
    "MediaGenerationNotConfiguredError",
    "MediaResult",
    "download_media_bytes",
    "get_ima_client",
    "get_media_cache",
    "generate_audio",
    "generate_image",
    "generate_video",
]
