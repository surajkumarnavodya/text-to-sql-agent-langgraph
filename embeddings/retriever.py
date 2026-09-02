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

Candidate scoring + adaptive selection (`_lexical_bonus`/
`_select_by_relevance`, the primary top-k step below): pure vector top-k
was previously a *fixed* count (`Settings.schema_top_k`, default 4)
regardless of how many tables a question actually needs. Measured against
this project's own benchmark, that's a dominant driver of low
relevant-table precision (24.2%, while recall was already 94.6%): the
benchmark's average question needs roughly 1.2 tables, so a fixed count of
4 mechanically pads every simple question with ~3 near-guaranteed-irrelevant
tables. Two changes address this *within the same top_k candidate window
the primary vector query already returns* -- deliberately not a wider pool
(see below for why that was tried and reverted):
(1) each candidate's vector similarity gets a small deterministic lexical
    bonus when a distinctive question word substring-matches the table's
    own name or a column name (same keyword extraction as the fallback
    above) -- this can only *re-rank within* the existing top_k window,
    never add a table that wasn't already a primary vector match, since
    it's additive to (not a replacement for) the vector score and the
    candidate pool itself isn't widened;
(2) the final count is only trimmed below `top_k` when the top-ranked
    candidate is *decisively* ahead of the runner-up (a large score gap)
    AND fewer than two candidates independently earned a lexical match
    (two independent literal name matches is itself strong evidence of a
    genuine multi-table question, even if one still leads the other by a
    wide margin -- see `_select_by_relevance`'s docstring for the concrete
    regression this guards against). Deliberately conservative, checked
    against this project's own retrieved score distributions (real
    fact-table pairs like FactInternetSales vs. FactResellerSales routinely
    score within ~0.02-0.05 of each other, too close to safely separate by
    any threshold). When there's no decisive winner, behavior is unchanged
    from before this rework: the full `top_k` candidates are kept.

Tried and reverted: widening the primary Chroma query to a pool larger than
`top_k` (so the lexical bonus could promote a table that wasn't even in the
raw top-k) and embedding `table_descriptions.yaml` business-term text
directly into each table's vector (see `embeddings/schema_indexer.py`'s
docstring for the concrete regression that caused). Measured live against
this project's own benchmark, the combination increased -- not decreased --
the average number of retrieved tables: a wider, noisier primary pool put
more, weakly-related candidates into the selected set, which then triggered
FK-bridge expansion far more often than intended (that mechanism is meant
for closing a rare structural gap between two already-good picks, not
stitching together several loosely-related ones). Recall, column-selection
accuracy, and latency all measurably regressed. Reverted to the bounded
version above, where the primary step can only return the same or fewer
tables than before, never more.
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

# Additive bonus (not a replacement for vector similarity) when a
# distinctive question keyword substring-matches the candidate's own table
# name, or one of its column names -- both are literal-identifier matches
# and treated as comparably strong evidence (a column name like
# "DiscountPct" matching "discount" is just as specific a signal as a table
# name match), applied flatly on presence of at least one match rather than
# scaled by how many of the question's *other* keywords (often generic
# quantitative language like "average" or "total", not schema terms) don't
# also match -- fraction-scaling was tried and under-rewarded a single
# strong, distinctive match.
_LEXICAL_TABLE_NAME_BONUS = 0.25
_LEXICAL_COLUMN_BONUS = 0.20

# The final selected count is trimmed below top_k only when the top-ranked
# candidate's (vector + lexical) score leads the runner-up by at least this
# much -- calibrated against this project's own retrieved score
# distributions (see this module's docstring): genuinely competing
# candidates (e.g. two structurally-similar fact tables) are routinely
# within ~0.05 of each other, so a materially larger gap is a real signal
# of a decisively single-table question, not noise.
_DOMINANT_GAP_THRESHOLD = 0.18

_COLUMN_LINE_RE = re.compile(r"^\s{4}(\w+)\s", re.MULTILINE)

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


def _column_names_from_ddl(ddl: str) -> list[str]:
    """Extracts column identifiers from a `render_ddl`-shaped text block.

    Matches only 4-space-indented lines (every real column line, per
    `db.schema_introspection.render_ddl`) starting with a bare identifier --
    this naturally skips the `CREATE TABLE (` / `);` bookend lines and
    `FOREIGN KEY (...) REFERENCES ...` lines (those start with `FOREIGN`,
    which still matches the regex as a "column name" token, so it's
    filtered explicitly below rather than relying on indentation alone).
    """
    return [
        match.group(1)
        for match in _COLUMN_LINE_RE.finditer(ddl)
        if match.group(1).upper() != "FOREIGN"
    ]


def _lexical_bonus(table_name: str, ddl: str, keywords: set[str]) -> float:
    """Additive relevance bonus from distinctive question keywords matching
    this table's own name or its column names -- see this module's
    docstring ("Candidate scoring + adaptive selection")."""
    if not keywords:
        return 0.0

    bonus = 0.0
    if any(keyword in table_name.lower() for keyword in keywords):
        bonus += _LEXICAL_TABLE_NAME_BONUS

    columns_lower = [c.lower() for c in _column_names_from_ddl(ddl)]
    if any(keyword in column for keyword in keywords for column in columns_lower):
        bonus += _LEXICAL_COLUMN_BONUS

    return bonus


def _select_by_relevance(
    candidates: list[TableSchema], top_k: int, lexically_matched_count: int
) -> list[TableSchema]:
    """Trims a combined-score-sorted candidate list to its adaptively-sized subset.

    `candidates` must already be sorted by `similarity_score` descending.
    Returns just the top candidate when it decisively outscores the
    runner-up (see `_DOMINANT_GAP_THRESHOLD`'s docstring note above);
    otherwise returns the first `top_k` unchanged from prior behavior.

    Never trims when 2+ candidates independently earned their own lexical
    match (`lexically_matched_count`), even if one still leads the other by
    a decisive margin -- e.g. "each employee's ... sales territory region"
    matches both DimEmployee ("employee") and DimSalesTerritory
    ("territory") by table name, but DimSalesTerritory's raw vector score
    happens to lead DimEmployee's by more than `_DOMINANT_GAP_THRESHOLD`
    regardless (a flat bonus added to both doesn't close a large
    pre-existing vector gap). Two independent, literal name matches is
    itself strong evidence of a genuine multi-table question -- trimming to
    one in that situation would be exactly the kind of recall regression
    this adaptive selection is meant to avoid.
    """
    if len(candidates) >= 2 and lexically_matched_count < 2:
        gap = candidates[0]["similarity_score"] - candidates[1]["similarity_score"]
        if gap >= _DOMINANT_GAP_THRESHOLD:
            return candidates[:1]
    return candidates[:top_k]


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
    """Returns up to `top_k` most relevant tables' DDL for `question`.

    Args:
        question: The user's natural-language question.
        top_k: Upper bound on tables to retrieve from the primary
            vector+lexical scoring step; defaults to `Settings.schema_top_k`.
            The actual count may be fewer than this (see
            `_select_by_relevance`) when one candidate decisively
            outscores the rest, and may end up higher after the
            keyword-match/FK-bridge expansion steps below add
            structurally- or lexically-necessary tables on top.
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
        # Deliberately queries exactly resolved_top_k candidates, not a
        # wider pool -- tried and reverted (see this module's docstring):
        # widening the pool so the lexical bonus could promote a weakly-
        # embedded table into contention also let more, noisier candidates
        # into the primary selection, which then triggered FK-bridge
        # expansion far more often than intended and measurably increased
        # (not decreased) the average retrieved-table count. Scoring and
        # adaptively trimming *within* the original top_k window is a
        # provably bounded change: the primary step can now only return the
        # same or fewer tables than before, never more.
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

    keywords = _extract_keywords(question)
    candidates: list[TableSchema] = []
    lexically_matched_count = 0
    for index, (doc, meta) in enumerate(zip(documents_row, metadatas_row, strict=True)):
        distance: float | None = distances_row[index] if index < len(distances_row) else None
        vector_similarity = round(1.0 - distance, 4) if distance is not None else 0.0
        raw_table_name = (meta or {}).get("table_name", "unknown")
        table_name = raw_table_name if isinstance(raw_table_name, str) else "unknown"
        bonus = _lexical_bonus(table_name, doc, keywords)
        if bonus > 0:
            lexically_matched_count += 1
        combined_score = round(vector_similarity + bonus, 4)
        candidates.append(
            TableSchema(table_name=table_name, ddl=doc, similarity_score=combined_score)
        )

    candidates.sort(key=lambda t: t["similarity_score"], reverse=True)
    tables = _select_by_relevance(candidates, resolved_top_k, lexically_matched_count)

    tables = _expand_with_keyword_matches(tables, question, collection)
    tables = _expand_with_fk_bridges(tables, collection)

    logger.debug(
        "Retrieved %d table(s) for question=%r: %s",
        len(tables),
        question,
        [t["table_name"] for t in tables],
    )
    return tables
