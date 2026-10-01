"""The governed semantic catalog's typed model -- Prompt 09
(`09_SEMANTIC_CATALOG_CONTRACT.md`): a persisted, tenant-scoped,
versioned business concept connecting technical metadata (tables,
columns, metrics) to business meaning (names, descriptions, grain, keys,
relationships, domains, dimensions, synonyms, business rules, examples),
with a real draft -> reviewed -> published -> superseded review workflow.

**Deliberately ORM-independent.** `CatalogEntrySnapshot` below is the
*only* shape `retrieval/` and `semantic/metrics.py` ever see -- neither
imports `identity.models.SemanticCatalogEntry` (the real, persisted ORM
row) or SQLAlchemy at all. This mirrors `onboarding/semantic_contract.py
::ConfirmedReviewItem`'s identical "minimal shape, no identity/ORM
dependency" precedent exactly, for the same reason: it keeps `retrieval/`
(already a dependency of `agent/`, the live request path) from ever
needing SQLAlchemy/`identity/` on its import graph, and keeps this
module's own logic plainly unit-testable with hand-built values.
`identity/repositories/semantic_catalog.py::entry_to_snapshot` is the one
place a real `SemanticCatalogEntry` row is converted into this shape.

**Status vocabulary is deliberately distinct from `semantic.metrics
.MetricStatus`** (draft/approved/deprecated) -- this module's
`CatalogStatus` is the exact draft/reviewed/published/superseded
vocabulary this prompt specifies, a real workflow with an intermediate
"reviewed but not yet live" state `MetricStatus` has no equivalent for.
`metric_definition_from_snapshot` is the one place the two vocabularies
are explicitly bridged (never conflated silently).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from agent.provenance import DataTruthLevel
from semantic.metrics import MetricDefinition, MetricStatus, YamlMetricRegistry


class CatalogConceptType(str, Enum):
    """The four kinds of business concept this catalog governs.

    A closed set, like `retrieval.models.ChunkType` -- an entry's
    `concept_type` never changes across its own version history (a new
    version of "customer_lifetime_value" is still a METRIC).
    """

    ENTITY = "entity"
    METRIC = "metric"
    DIMENSION = "dimension"
    DOMAIN = "domain"


class CatalogStatus(str, Enum):
    """A catalog entry's review-workflow state -- exactly the vocabulary
    this prompt specifies.

    Attributes:
        DRAFT: Authored (by a human or an automated inference pass, e.g.
            a future onboarding-engine integration), not yet reviewed.
            Mutable in place -- `identity.repositories.semantic_catalog
            .update_draft_entry` only ever operates on a `DRAFT` row.
        REVIEWED: An SME has reviewed and approved the content, but it is
            not yet live in retrieval. Exists as its own state (rather
            than collapsing review-approval and publish into one step)
            specifically so a reviewed-but-not-yet-published entry can be
            scheduled/batched for publish separately from the review
            decision itself.
        PUBLISHED: Live -- the **only** status `retrieval/`'s vector
            store ever indexes a chunk for (see `retrieval.chunking
            .business_concept_chunk_from_catalog_entry`'s own docstring).
            Immutable once reached; a further edit always creates a new
            `DRAFT` row (a new version), never mutates a published row.
        SUPERSEDED: A previously `PUBLISHED` row that a newer version of
            the same concept has replaced. Retained (never deleted) so a
            concept's full version history stays inspectable, mirroring
            `identity.models.OnboardingArtifact`'s append-only
            versioning. Its vector-store chunk is removed on supersession
            (see `retrieval.ingestion.sync_catalog_entry_to_vector_store`).
    """

    DRAFT = "draft"
    REVIEWED = "reviewed"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


#: Transitions `identity/repositories/semantic_catalog.py` enforces --
#: the single source of truth for "which status can move to which,"
#: checked by both the repository layer (defense in depth) and
#: `api/semantic_catalog.py` (the user-facing 409 before a repository
#: call is even attempted).
VALID_STATUS_TRANSITIONS: dict[CatalogStatus, frozenset[CatalogStatus]] = {
    CatalogStatus.DRAFT: frozenset({CatalogStatus.REVIEWED}),
    CatalogStatus.REVIEWED: frozenset({CatalogStatus.DRAFT, CatalogStatus.PUBLISHED}),
    CatalogStatus.PUBLISHED: frozenset({CatalogStatus.SUPERSEDED}),
    CatalogStatus.SUPERSEDED: frozenset(),
}


def status_to_truth_level(status: CatalogStatus) -> DataTruthLevel:
    """Maps a catalog entry's workflow state onto the shared
    `agent.provenance.DataTruthLevel` vocabulary -- the structural
    enforcement of master-contract rules 9-10 for this catalog: a
    `DRAFT`/`REVIEWED` entry is always `AI_INFERENCE` (not yet confirmed,
    regardless of how confident its own `confidence` field is), and only
    `PUBLISHED` (which requires having passed through `REVIEWED` first --
    see `VALID_STATUS_TRANSITIONS`) is ever `CONFIRMED_BUSINESS_TRUTH`.
    `SUPERSEDED` deliberately reverts to `AI_INFERENCE` rather than
    keeping `CONFIRMED_BUSINESS_TRUTH` -- a retired claim is no longer
    the live, confirmed answer, and must never be re-promoted implicitly
    just because it once was.
    """
    if status == CatalogStatus.PUBLISHED:
        return DataTruthLevel.CONFIRMED_BUSINESS_TRUTH
    return DataTruthLevel.AI_INFERENCE


class CatalogEntrySnapshot(BaseModel):
    """The ORM-independent read shape of one `identity.models
    .SemanticCatalogEntry` row -- see this module's own docstring for why.

    `keys`/`relationships`/`synonyms`/`business_rules`/`examples`/
    `evidence` default to empty -- not every concept type populates every
    field (a `DOMAIN` entry typically has no `grain`/`keys`; a `METRIC`
    entry typically has no `relationships`).

    `approved_expression`/`source_tables`/`filters`/`dimensions`/
    `aggregation` (Prompt 10, `10_GOVERNED_METRICS_CONTRACT.md`) are the
    fields that make a `METRIC`-type entry *governing*, not merely
    descriptive: `approved_expression` is the authoritative SQL-
    expression-shaped definition (e.g. `"SUM(SalesAmount) /
    COUNT(DISTINCT SalesOrderNumber)"`) SQL generation must prefer over
    anything it would otherwise invent (see `agent.llm_client
    ._build_mandatory_metrics_block` and `agent.nodes
    .review_metric_conformance_node`) -- deliberately a separate field
    from `technical_name` (a short label, e.g. a column/table name for
    non-metric concepts) rather than overloading it. `source_tables` is
    deliberately distinct from `keys` (join/identifier keys, not "which
    tables this metric reads"). Every one of these five defaults to
    empty/`None` for a non-metric concept type -- not every concept type
    populates them, same convention as the Prompt-09 fields above.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    database_id: str
    concept_type: CatalogConceptType
    concept_key: str
    business_name: str
    technical_name: str | None = None
    description: str = ""
    grain: str | None = None
    keys: tuple[str, ...] = ()
    relationships: tuple[dict[str, Any], ...] = ()
    domain: str | None = None
    synonyms: tuple[str, ...] = ()
    business_rules: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    confidence: float = 1.0
    status: CatalogStatus
    owner: str | None = None
    version: int = 1
    approved_expression: str | None = None
    source_tables: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    aggregation: str | None = None

    @property
    def truth_level(self) -> DataTruthLevel:
        return status_to_truth_level(self.status)


def metric_definition_from_snapshot(snapshot: CatalogEntrySnapshot) -> MetricDefinition | None:
    """Bridges a `METRIC`-type catalog entry into `semantic.metrics
    .MetricDefinition` -- `None` for every other `concept_type`.

    This is the explicit, disclosed bridge between this prompt's
    draft/reviewed/published/superseded vocabulary and `MetricDefinition
    .status`'s existing draft/approved/deprecated one (`02_TARGET_
    ARCHITECTURE.md` §5's named gap "(b) a real approval-workflow/CRUD
    surface sets owner/APPROVED" -- this function is exactly that
    surface): `PUBLISHED` -> `APPROVED` (a published entry has passed
    real human review, the same bar `MetricStatus.APPROVED`'s own
    docstring names), `DRAFT`/`REVIEWED` -> `DRAFT` (neither is live yet),
    `SUPERSEDED` -> `DEPRECATED` (retired, still resolvable for historical
    queries -- `MetricStatus.DEPRECATED`'s own documented meaning).

    **Prompt 10** (`10_GOVERNED_METRICS_CONTRACT.md`): `formula`/
    `aggregation`/`valid_filters`/`source_tables` now map from the
    snapshot's own real `approved_expression`/`aggregation`/`filters`/
    `source_tables` fields -- Prompt 09's version of this function had
    nothing real to put there and hard-coded them empty; those fields
    didn't exist on the catalog yet.
    """
    if snapshot.concept_type != CatalogConceptType.METRIC:
        return None
    status_map = {
        CatalogStatus.DRAFT: MetricStatus.DRAFT,
        CatalogStatus.REVIEWED: MetricStatus.DRAFT,
        CatalogStatus.PUBLISHED: MetricStatus.APPROVED,
        CatalogStatus.SUPERSEDED: MetricStatus.DEPRECATED,
    }
    return MetricDefinition(
        name=snapshot.business_name,
        definition=snapshot.description,
        formula=snapshot.approved_expression or "",
        aggregation=snapshot.aggregation or "",
        grain=snapshot.grain or "",
        valid_filters=snapshot.filters,
        source_tables=snapshot.source_tables,
        source_columns=(),
        time_period_interpretation="",
        tags=snapshot.synonyms,
        owner=snapshot.owner,
        status=status_map[snapshot.status],
        version=snapshot.version,
    )


def build_metric_registry_from_catalog(
    entries: list[CatalogEntrySnapshot],
) -> YamlMetricRegistry:
    """Builds a `MetricRegistry` (the existing Protocol -- see
    `semantic.metrics`) from a list of catalog snapshots, reusing
    `YamlMetricRegistry`'s own constructor as a second, richer data
    source rather than inventing a new registry class. Entries that
    aren't `METRIC`-typed, or have no `metric_definition_from_snapshot`
    mapping, are silently skipped (not every catalog entry is a metric).

    Only ever called with **published** entries in practice (the
    caller's responsibility, same as `retrieval.chunking
    .business_concept_chunk_from_catalog_entry`'s own "only published
    reaches a consumer" contract) -- this function itself places no
    status filter, since a caller inspecting draft content for review
    purposes is a legitimate, different use case.
    """
    definitions = tuple(
        definition
        for definition in (metric_definition_from_snapshot(entry) for entry in entries)
        if definition is not None
    )
    return YamlMetricRegistry(definitions)
