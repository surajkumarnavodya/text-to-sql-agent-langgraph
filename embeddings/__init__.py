"""ChromaDB-backed schema indexing and retrieval, for prompt-size schema scoping."""

from embeddings.retriever import retrieve_relevant_schema
from embeddings.schema_indexer import build_index

__all__ = ["build_index", "retrieve_relevant_schema"]
