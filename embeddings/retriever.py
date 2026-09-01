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

Keyword-match fallback (`_expand_with_keyword_matches`, runs before the
FK-bridge step): FK-bridging only helps when a needed table is a
*structural gap* between tables that already were retrieved -- it does
nothing if the needed table simply never made it into the retrieved set at
all and isn't required to connect anything else. A table whose *embedded*
DDL is dominated by long, low-signal columns (free-text descriptions in
several languages, binary blobs, coded fields) can rank far outside top_k
even when its own table name is a direct, obvious match for the question
-- observed concretely with `DimProduct` ranking outside the top 15 of 31
tables for a question containing the word "product." A cheap, bounded,
deterministic substring match of distinctive question words against table
*names* (not columns, not DDL content) catches this specific class of miss
without turning into a second, redundant retrieval strategy.
"""

from __future__ import annotations

import logging
import re
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

# Keyword-match fallback is bounded the same way the FK-bridge budget is
# (see _MAX_BRIDGE_TABLES) -- a schema with many similarly-named tables
# (DimProduct, DimProductCategory, DimProductSubcategory, ...) must not
# have all of them dragged in just because one word matched.
_MAX_KEYWORD_MATCHES = 3

# A word shorter than this is too generic to safely substring-match a table
# name on its own (e.g. "id", "key", "by").
_MIN_KEYWORD_LENGTH = 4

# Common question words, and terms generic enough that pure vector
# similarity already handles them well (broad concepts like "sales" or
# "date" match many tables' embeddings directly), excluded so the fallback
# only fires for genuinely distinctive terms it's actually needed for.
_KEYWORD_STOPWORDS = frozenset(
    {
        "show",
        "list",
        "find",
        "give",
        "many",
        "much",
        "what",
        "which",
        "when",
        "where",
        "were",
        "have",
        "does",
        "with",
        "that",
        "this",
        "from",
        "total",
        "totals",
        "table",
        "tables",
        "data",
        "value",
        "values",
        "count",
        "counts",
        "number",
        "amount",
        "amounts",
        "sales",
        "date",
        "dates",
        "year",
        "years",
    }
)

_WORD_RE = re.compile(r"[a-zA-Z]+")


def _extract_keywords(question: str) -> set[str]:
    """Distinctive, lowercased words from `question` worth substring-matching
    against table names -- see `_expand_with_keyword_matches`."""
    words = _WORD_RE.findall(question.lower())
    return {w for w in words if len(w) >= _MIN_KEYWORD_LENGTH and w not in _KEYWORD_STOPWORDS}


def _expand_with_keyword_matches(
    selected: list[TableSchema], question: str, collection: Collection
) -> list[TableSchema]:
    """Adds tables whose own name plainly contains a distinctive question word.

    A cheap, deterministic fallback underneath pure vector similarity. A
    table's *embedded* DDL can be dominated by long, semantically-noisy
    columns (free-text descriptions in several languages, binary blobs,
    coded measurement fields) that dilute its embedding far more than its
    short, on-topic table name alone would suggest -- concretely observed:
    `DimProduct` (whose DDL includes nine language-variant description
    columns plus size/weight/measurement-code fields) ranked outside the
    top 15 of 31 tables for a question containing the word "product",
    despite "product" being a literal substring of the table's own name.
    That caused the agent to invent plausible-but-wrong column names for a
    table it was never actually shown, rather than admit it didn't know.

    Table-name-only (not column names) and budget-capped (see
    `_MAX_KEYWORD_MATCHES`), to stay precise -- this is a narrow, deliberate
    fallback for the specific failure mode above, not a second retrieval
    strategy. Runs before FK-bridge expansion so a structural gap introduced
    by a keyword match (e.g. matching a dimension but not the fact table
    that joins it) can still be closed afterward.
    """
    keywords = _extract_keywords(question)
    if not keywords:
        return selected

    selected_names = {t["table_name"] for t in selected}
    try:
        # Untyped as `Any`, matching `_build_fk_adjacency`'s own treatment of
        # this same Chroma metadata elsewhere in this module -- Chroma's
        # `Metadata` value type is a broad union (str/int/float/bool/...) at
        # the stub level, but this app only ever writes `table_name` as a
        # plain str (`schema_indexer.py`), so treating it as one here is a
        # deliberate, established simplification, not a real type hazard.
        all_metadatas: list[Any] = collection.get(include=["metadatas"]).get("metadatas") or []
    except Exception as exc:  # noqa: BLE001 - best-effort enrichment, never fatal
        logger.warning("Keyword-match expansion skipped (could not read metadata): %s", exc)
        return selected

    candidates = sorted(
        {
            meta["table_name"]
            for meta in all_metadatas
            if meta.get("table_name")
            and meta["table_name"] not in selected_names
            and any(keyword in meta["table_name"].lower() for keyword in keywords)
        }
    )
    added = candidates[:_MAX_KEYWORD_MATCHES]
    if not added:
        return selected

    logger.info("[retrieve_schema] Keyword-match expansion added: %s", added)
    fetched = collection.get(ids=added, include=["documents"])
    fetched_documents = fetched.get("documents") or []
    keyword_tables = [
        TableSchema(table_name=table_name, ddl=document, similarity_score=0.0)
        for table_name, document in zip(fetched["ids"], fetched_documents, strict=True)
    ]
    return selected + keyword_tables


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

    tables = _expand_with_keyword_matches(tables, question, collection)
    tables = _expand_with_fk_bridges(tables, collection)

    logger.debug(
        "Retrieved %d table(s) for question=%r: %s",
        len(tables),
        question,
        [t["table_name"] for t in tables],
    )
    return tables
