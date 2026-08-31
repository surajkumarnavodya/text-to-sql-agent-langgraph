"""Top-k relevant-table retrieval from the Chroma schema index.

This is what keeps the LLM's prompt small on a large schema: instead of
dumping every table's DDL into every request, the question is embedded and
matched against per-table DDL chunks, and only the closest `top_k` tables
are returned to the caller (`agent.nodes.retrieve_schema_node`).

FK-adjacency bridge expansion: pure similarity top-k can miss a table that
has no descriptive-sounding column names but is structurally required to
connect tables that *were* retrieved -- most commonly a fact table (mostly
numeric columns, so it embeds weakly against a question like "which
territory had the highest sales") or an intermediate dimension in a multi-
level hierarchy (e.g. DimProduct, sitting between a fact table and
DimProductSubcategory/DimProductCategory). Left alone, the LLM ends up
inventing a direct column that doesn't exist, or joining two unrelated
surrogate keys, to skip the missing hop or hops.

The expansion works by connected-component merging over the *full*
FK graph, not just a local adjacency-count heuristic: if the retrieved
tables form more than one disconnected island in the FK graph, the
shortest real path between the two closest islands is found (via BFS) and
its intermediate tables are added, repeating until everything retrieved is
one connected component (or the bridge budget runs out). This is what
correctly handles *two or more consecutive* missing hops -- e.g. both
DimProduct and the fact table missing at once -- which a single-hop
"adjacent to 2+ selected tables" heuristic can't bootstrap into, since
neither missing table has 2 selected neighbors until the other is already
present.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from chromadb.api.models.Collection import Collection

from agent.exceptions import SchemaRetrievalError
from agent.state import TableSchema
from config.settings import Settings, get_settings
from embeddings.schema_indexer import get_chroma_client, get_collection

logger = logging.getLogger(__name__)

# Bridge tables are added at most this many in total, so a large schema's
# retrieval can't balloon back up to "dump everything" -- defeating the
# entire point of top-k scoping -- just because many tables happen to be
# interconnected. Deliberately a little more than the minimum any single
# observed case has needed: when two candidate bridge paths tie in length,
# the tie-break (alphabetical, for determinism) can pick the less-useful
# one, "spending" a slot that a stricter minimum wouldn't have spared for
# the table that was actually needed next.
_MAX_BRIDGE_TABLES = 5

# A connecting path requiring more intermediate tables than this is treated
# as "these two tables aren't really related for this question" rather than
# force-bridged -- without this, two coincidentally-retrieved but unrelated
# tables could drag in a long, irrelevant chain of connector tables.
_MAX_BRIDGE_PATH_HOPS = 3


def _build_fk_adjacency(all_metadatas: list[Any]) -> dict[str, set[str]]:
    """Builds an undirected table adjacency map from stored `fk_targets` metadata."""
    adjacency: dict[str, set[str]] = {}
    for metadata in all_metadatas:
        table_name = metadata.get("table_name")
        if not table_name:
            continue
        adjacency.setdefault(table_name, set())
        targets = metadata.get("fk_targets") or ""
        for target in targets.split(","):
            target = target.strip()
            if not target:
                continue
            adjacency[table_name].add(target)
            adjacency.setdefault(target, set()).add(table_name)
    return adjacency


def _connected_components(nodes: set[str], adjacency: dict[str, set[str]]) -> list[set[str]]:
    """Connected components of the subgraph induced by `nodes` (edges only within `nodes`)."""
    remaining = set(nodes)
    components: list[set[str]] = []
    while remaining:
        start = min(remaining)  # deterministic choice of starting node
        component = {start}
        frontier = [start]
        remaining.discard(start)
        while frontier:
            current = frontier.pop()
            for neighbor in adjacency.get(current, set()):
                if neighbor in remaining:
                    remaining.discard(neighbor)
                    component.add(neighbor)
                    frontier.append(neighbor)
        components.append(component)
    return components


def _shortest_path(
    start_nodes: set[str], end_nodes: set[str], adjacency: dict[str, set[str]]
) -> list[str] | None:
    """BFS shortest path from any node in `start_nodes` to any node in `end_nodes`.

    Searches the *full* FK graph (not just already-selected tables), since
    the whole point is to find a real connector that wasn't retrieved.
    """
    visited = set(start_nodes)
    queue: deque[tuple[str, list[str]]] = deque((node, [node]) for node in sorted(start_nodes))
    while queue:
        current, path = queue.popleft()
        for neighbor in sorted(adjacency.get(current, set())):
            if neighbor in end_nodes:
                return path + [neighbor]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))
    return None


def _expand_with_fk_bridges(
    selected: list[TableSchema], collection: Collection
) -> list[TableSchema]:
    """Merges disconnected islands in the retrieved set via real FK paths.

    Repeatedly finds the two closest disconnected components among the
    retrieved tables (by real shortest-path distance in the full FK graph)
    and adds the intermediate tables on that path, until everything is one
    component, a path is too long to be worth force-connecting, or the
    bridge budget (`_MAX_BRIDGE_TABLES`) runs out.
    """
    selected_names = {t["table_name"] for t in selected}
    try:
        all_metadatas = collection.get(include=["metadatas"]).get("metadatas") or []
    except Exception as exc:  # noqa: BLE001 - best-effort enrichment, never fatal
        logger.warning("FK-bridge expansion skipped (could not read metadata): %s", exc)
        return selected

    adjacency = _build_fk_adjacency(all_metadatas)
    added: list[str] = []
    components = _connected_components(selected_names, adjacency)

    while len(components) > 1 and len(added) < _MAX_BRIDGE_TABLES:
        best_path: list[str] | None = None
        for i, component_a in enumerate(components):
            for component_b in components[i + 1 :]:
                path = _shortest_path(component_a, component_b, adjacency)
                if path is not None and (best_path is None or len(path) < len(best_path)):
                    best_path = path
        if best_path is None:
            break  # no path exists in the FK graph at all between some components

        intermediate = [node for node in best_path if node not in selected_names]
        if len(intermediate) > _MAX_BRIDGE_PATH_HOPS:
            break  # too far apart to be worth force-connecting

        for node in intermediate:
            if len(added) >= _MAX_BRIDGE_TABLES:
                break
            selected_names.add(node)
            added.append(node)
        components = _connected_components(selected_names, adjacency)

    if not added:
        return selected

    logger.info("[retrieve_schema] FK-bridge expansion added: %s", added)
    fetched = collection.get(ids=added, include=["documents"])
    fetched_documents = fetched.get("documents") or []
    bridge_tables = [
        TableSchema(table_name=table_name, ddl=document, similarity_score=0.0)
        for table_name, document in zip(fetched["ids"], fetched_documents, strict=True)
    ]
    return selected + bridge_tables


def retrieve_relevant_schema(
    question: str,
    top_k: int | None = None,
    settings: Settings | None = None,
) -> list[TableSchema]:
    """Returns the `top_k` most relevant tables' DDL for `question`.

    Args:
        question: The user's natural-language question.
        top_k: Number of tables to retrieve; defaults to `Settings.schema_top_k`.
        settings: Optional `Settings` override (mainly for tests).

    Returns:
        A list of `TableSchema` (table_name, ddl, similarity_score), ordered
        most-relevant first. Empty if the index has no tables at all -- the
        caller decides how to handle that (see `retrieve_schema_node`).

    Raises:
        SchemaRetrievalError: if the Chroma index is missing/unbuilt, or the
            query otherwise fails (e.g. embedding backend not available).
    """
    settings = settings or get_settings()
    resolved_top_k = top_k or settings.schema_top_k

    try:
        client = get_chroma_client(settings)
        collection = get_collection(client, settings)
        if collection.count() == 0:
            raise SchemaRetrievalError(
                "The schema index is empty. Run `python scripts/build_embeddings.py` first."
            )
        result = collection.query(
            query_texts=[question],
            n_results=min(resolved_top_k, collection.count()),
        )
    except SchemaRetrievalError:
        raise
    except Exception as exc:  # noqa: BLE001 - Chroma/onnxruntime error types vary by backend
        raise SchemaRetrievalError(f"Failed to query the schema index: {exc}") from exc

    documents = result.get("documents") or [[]]
    metadatas = result.get("metadatas") or [[]]
    distances = result.get("distances") or [[]]

    documents_row = documents[0] if documents else []
    metadatas_row = metadatas[0] if metadatas else []
    distances_row = distances[0] if distances else []

    tables: list[TableSchema] = []
    for index, (doc, meta) in enumerate(zip(documents_row, metadatas_row, strict=True)):
        distance: float | None = distances_row[index] if index < len(distances_row) else None
        similarity = round(1.0 - distance, 4) if distance is not None else 0.0
        raw_table_name = (meta or {}).get("table_name", "unknown")
        table_name = raw_table_name if isinstance(raw_table_name, str) else "unknown"
        tables.append(TableSchema(table_name=table_name, ddl=doc, similarity_score=similarity))

    tables = _expand_with_fk_bridges(tables, collection)

    logger.debug(
        "Retrieved %d table(s) for question=%r: %s",
        len(tables),
        question,
        [t["table_name"] for t in tables],
    )
    return tables
