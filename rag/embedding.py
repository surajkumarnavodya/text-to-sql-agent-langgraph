"""Shared embedding function for RAG chunk/query text -- used by both
`rag/ingestion.py` (embedding chunks at upload time) and `rag/retriever.py`
(embedding the question at query time), so the two are guaranteed to use the
identical model/vector space.
"""

from __future__ import annotations

from chromadb.utils import embedding_functions

from config.settings import Settings


def embed_texts(texts: list[str], settings: Settings) -> list[list[float]]:
    """Embeds a batch of texts with the configured RAG embedding model.

    Reuses `embeddings.schema_indexer`'s exact embedding-function choice
    (Chroma's bundled `DefaultEmbeddingFunction` for the default model, or
    `SentenceTransformerEmbeddingFunction` for a non-default one) rather than
    a separate embedding stack -- one model download/runtime for the whole
    app. `Settings.rag_embedding_model_name` overrides
    `Settings.embedding_model_name` only for this path, if genuinely needed;
    blank (the default) means "use the same model schema retrieval uses."
    """
    model_name = settings.rag_embedding_model_name or settings.embedding_model_name
    embedding_function: embedding_functions.EmbeddingFunction
    if model_name == "all-MiniLM-L6-v2":
        embedding_function = embedding_functions.DefaultEmbeddingFunction()
    else:
        embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=model_name
        )
    raw = embedding_function(texts)
    return [[float(x) for x in vector] for vector in raw]
