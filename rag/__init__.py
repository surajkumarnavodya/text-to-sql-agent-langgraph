"""Document/policy agentic RAG -- PDF ingestion, SQL Server vector storage,
retrieval, and the retrieve-grade-rewrite-generate subgraph.

Two independent collections share this exact same code, parameterized by
name: "documents" (general PDF uploads) and "policies" (internal company
policy PDFs, more access-sensitive -- see `rag/store.py`'s
`sensitivity_category` column). Kept structurally identical rather than two
near-duplicate modules, per the original multi-source design's own framing.

See:
  - `rag/store.py` -- SQL Server schema (native `VECTOR` columns) and storage.
  - `rag/ingestion.py` -- PDF extract -> chunk -> embed -> store pipeline.
  - `rag/retriever.py` -- top-k retrieval + LLM relevance grading + rewrite.
  - `rag/graph.py` -- the agentic subgraph (`build_rag_subgraph`), consumed
    by `agent/orchestrator/nodes.py`.
"""
