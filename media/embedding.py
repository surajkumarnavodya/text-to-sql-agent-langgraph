"""Text/image embedding for media search -- provider-configurable, shaped
like `search/web_search.py`'s `SUPPORTED_SEARCH_PROVIDERS` pattern
(`Settings.media_embedding_provider` selects the implementation; adding a
hosted provider later -- Voyage multimodal-3, Vertex multimodalembedding,
OpenAI -- is a new dict entry here, not a redesign of any call site).

Only `"local_clip"` is implemented today: a `sentence-transformers` CLIP
checkpoint (default `clip-ViT-B-32`) running fully on-device -- no API key,
no per-item cost, no image ever leaving the machine, consistent with this
project's Ollama/faster-whisper/Piper local-first posture. The SAME model
instance embeds both images and query text (`embed_image`/`embed_text`),
which is what guarantees they land in one comparable vector space -- that
shared space is CLIP's entire point, and is why this module deliberately
does not delegate to Chroma's own `EmbeddingFunction` callback interface
(text-only) the way `embeddings/schema_indexer.py`/`rag/embedding.py` do --
see `media/store.py`'s module docstring.

This is the one real exception to this project's "no torch" dependency
posture (see `requirements.txt`'s comment on this) -- `sentence-transformers`
needs it for local CLIP inference.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from config.settings import Settings
from media.exceptions import MediaEmbeddingModelNotFoundError

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


@cache
def _load_clip_model(model_name: str) -> SentenceTransformer:
    """Process-lifetime-cached CLIP model load -- same `functools.cache`
    singleton idiom as `agent.llm_client._get_ollama_client`/
    `voice.stt._get_whisper_model`. Loading a `SentenceTransformer` is a
    real, multi-second cost (weights download on first use, cached by
    Hugging Face Hub after that) that must not repeat per call.
    """
    from sentence_transformers import SentenceTransformer

    try:
        return SentenceTransformer(model_name)
    except Exception as exc:  # noqa: BLE001 - huggingface_hub/torch error types vary
        raise MediaEmbeddingModelNotFoundError(
            f"Failed to load CLIP model {model_name!r}: {exc}"
        ) from exc


def _local_clip_embed_text(text: str, settings: Settings) -> list[float]:
    model = _load_clip_model(settings.media_clip_model_name)
    vector = model.encode(text, convert_to_numpy=True, normalize_embeddings=True)
    return [float(x) for x in vector]


def _local_clip_embed_image(image_path: Path, settings: Settings) -> list[float]:
    from PIL import Image

    model = _load_clip_model(settings.media_clip_model_name)
    with Image.open(image_path) as raw_image:
        rgb_image = raw_image.convert("RGB")
        vector = model.encode(rgb_image, convert_to_numpy=True, normalize_embeddings=True)
    return [float(x) for x in vector]


# provider name -> embed function. A hosted provider would add its own
# entry to both maps (each responsible for its own auth/quota handling) --
# `embed_text`/`embed_image` below never need to change.
_TEXT_PROVIDERS: dict[str, Callable[[str, Settings], list[float]]] = {
    "local_clip": _local_clip_embed_text,
}
_IMAGE_PROVIDERS: dict[str, Callable[[Path, Settings], list[float]]] = {
    "local_clip": _local_clip_embed_image,
}


def embed_text(text: str, settings: Settings) -> list[float]:
    """Embeds a query (or a video segment's dense caption/transcript/OCR
    text) into the configured provider's vector space."""
    return _TEXT_PROVIDERS[settings.media_embedding_provider](text, settings)


def embed_image(image_path: Path, settings: Settings) -> list[float]:
    """Embeds one image (or extracted video keyframe) file into the same
    vector space `embed_text` uses."""
    return _IMAGE_PROVIDERS[settings.media_embedding_provider](image_path, settings)
