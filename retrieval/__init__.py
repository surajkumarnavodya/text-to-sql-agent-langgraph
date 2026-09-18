"""Business-context vector retrieval layer.

Adds semantic retrieval of table/column/relationship descriptions, business
glossary terms, metric definitions, curated SQL examples, and documentation
snippets -- on top of, never instead of, the live schema introspection that
already scopes `agent.graph`'s SQL generation (`embeddings/schema_indexer.py`
+ `embeddings/retriever.py`).

This package deliberately reuses the project's existing ChromaDB
infrastructure (`embeddings.schema_indexer.get_chroma_client`, the same
cached `PersistentClient` singleton every other Chroma-backed module here
already shares) rather than introducing a second vector database -- see
`docs/vector-retrieval-design.md`'s "Selected vector database" section for
the full reasoning.

Module map:
    models.py    -- typed chunk model + stable, deterministic chunk IDs.
    chunking.py  -- turns introspected schema + `data/knowledge/*` into chunks.
    embeddings.py -- embedding-provider abstraction (local, or a fake for tests).
    vector_store.py -- the vector-store interface + its Chroma implementation.
    reranker.py  -- deterministic post-retrieval scoring/diversity.
    retriever.py -- the retrieval orchestration called from the LangGraph node.
    ingestion.py -- idempotent discover -> chunk -> hash -> embed -> upsert pipeline.

Every public entry point in this package is fail-open: a vector-store
failure, a missing collection, or a misconfigured embedding provider must
degrade to "no extra context" (plus a logged, state-visible warning), never
raise into the middle of a running SQL generation. See `retriever.py`'s
module docstring for the full fallback contract.
"""
