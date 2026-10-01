"""Unit tests for semantic/catalog.py (Prompt 09,
`09_SEMANTIC_CATALOG_CONTRACT.md`) -- the governed semantic catalog's
typed model. Fully offline: plain `CatalogEntrySnapshot` values in, plain
values/`MetricDefinition`/`YamlMetricRegistry` out, no DB, no HTTP.
"""

from __future__ import annotations

from semantic.catalog import (
    VALID_STATUS_TRANSITIONS,
    CatalogConceptType,
    CatalogEntrySnapshot,
    CatalogStatus,
    build_metric_registry_from_catalog,
    metric_definition_from_snapshot,
    status_to_truth_level,
)
from semantic.metrics import MetricStatus

from agent.provenance import DataTruthLevel


def _snapshot(
    concept_type=CatalogConceptType.ENTITY,
    status=CatalogStatus.DRAFT,
    **overrides,
) -> CatalogEntrySnapshot:
    defaults = dict(
        id="entry-1",
        tenant_id="tenant-a",
        database_id="db1",
        concept_type=concept_type,
        concept_key="customer",
        business_name="Customer",
        status=status,
    )
    defaults.update(overrides)
    return CatalogEntrySnapshot(**defaults)


class TestStatusToTruthLevel:
    def test_draft_is_ai_inference(self):
        assert status_to_truth_level(CatalogStatus.DRAFT) == DataTruthLevel.AI_INFERENCE

    def test_reviewed_is_ai_inference(self):
        """Reviewed-but-not-yet-published is still not a confirmed
        truth -- the structural enforcement of master-contract rule 10."""
        assert status_to_truth_level(CatalogStatus.REVIEWED) == DataTruthLevel.AI_INFERENCE

    def test_published_is_confirmed_business_truth(self):
        assert (
            status_to_truth_level(CatalogStatus.PUBLISHED)
            == DataTruthLevel.CONFIRMED_BUSINESS_TRUTH
        )

    def test_superseded_reverts_to_ai_inference_never_re_promoted(self):
        assert status_to_truth_level(CatalogStatus.SUPERSEDED) == DataTruthLevel.AI_INFERENCE

    def test_snapshot_truth_level_property_matches_the_function(self):
        snapshot = _snapshot(status=CatalogStatus.PUBLISHED)
        assert snapshot.truth_level == DataTruthLevel.CONFIRMED_BUSINESS_TRUTH


class TestValidStatusTransitions:
    def test_draft_can_only_move_to_reviewed(self):
        assert VALID_STATUS_TRANSITIONS[CatalogStatus.DRAFT] == frozenset({CatalogStatus.REVIEWED})

    def test_reviewed_can_move_to_draft_or_published(self):
        assert VALID_STATUS_TRANSITIONS[CatalogStatus.REVIEWED] == frozenset(
            {CatalogStatus.DRAFT, CatalogStatus.PUBLISHED}
        )

    def test_published_can_only_move_to_superseded(self):
        assert VALID_STATUS_TRANSITIONS[CatalogStatus.PUBLISHED] == frozenset(
            {CatalogStatus.SUPERSEDED}
        )

    def test_superseded_is_terminal(self):
        assert VALID_STATUS_TRANSITIONS[CatalogStatus.SUPERSEDED] == frozenset()


class TestCatalogEntrySnapshotGovernedMetricFields:
    """Prompt 10 (`10_GOVERNED_METRICS_CONTRACT.md`)'s new fields."""

    def test_defaults_are_empty_for_a_non_metric_entry(self):
        snapshot = _snapshot(concept_type=CatalogConceptType.ENTITY)
        assert snapshot.approved_expression is None
        assert snapshot.source_tables == ()
        assert snapshot.filters == ()
        assert snapshot.dimensions == ()
        assert snapshot.aggregation is None

    def test_a_metric_entry_can_carry_every_governed_field(self):
        snapshot = _snapshot(
            concept_type=CatalogConceptType.METRIC,
            approved_expression="SUM(SalesAmount) / COUNT(DISTINCT SalesOrderNumber)",
            source_tables=("FactInternetSales",),
            filters=("OrderDateKey", "Region"),
            dimensions=("Region", "ProductCategory"),
            aggregation="derived ratio",
        )
        assert snapshot.approved_expression == (
            "SUM(SalesAmount) / COUNT(DISTINCT SalesOrderNumber)"
        )
        assert snapshot.source_tables == ("FactInternetSales",)
        assert snapshot.filters == ("OrderDateKey", "Region")
        assert snapshot.dimensions == ("Region", "ProductCategory")
        assert snapshot.aggregation == "derived ratio"


class TestMetricDefinitionFromSnapshot:
    def test_non_metric_concept_type_returns_none(self):
        snapshot = _snapshot(concept_type=CatalogConceptType.ENTITY)
        assert metric_definition_from_snapshot(snapshot) is None

    def test_metric_concept_type_maps_every_field(self):
        snapshot = _snapshot(
            concept_type=CatalogConceptType.METRIC,
            concept_key="clv",
            business_name="Customer Lifetime Value",
            description="Total historical revenue per customer",
            grain="one row per customer",
            synonyms=("CLV",),
            owner="alice",
            version=3,
            status=CatalogStatus.PUBLISHED,
            # Prompt 10 (10_GOVERNED_METRICS_CONTRACT.md) fields -- these,
            # not `technical_name`/`keys`, are what `formula`/
            # `source_tables`/`aggregation`/`valid_filters` now map from.
            approved_expression="SUM(Amount)",
            source_tables=("FactSales",),
            filters=("Region",),
            aggregation="SUM",
        )
        definition = metric_definition_from_snapshot(snapshot)
        assert definition is not None
        assert definition.name == "Customer Lifetime Value"
        assert definition.formula == "SUM(Amount)"
        assert definition.definition == "Total historical revenue per customer"
        assert definition.grain == "one row per customer"
        assert definition.source_tables == ("FactSales",)
        assert definition.valid_filters == ("Region",)
        assert definition.aggregation == "SUM"
        assert definition.tags == ("CLV",)
        assert definition.owner == "alice"
        assert definition.version == 3

    def test_metric_with_no_approved_expression_maps_to_an_empty_formula(self):
        """A METRIC-type entry that hasn't had its governed fields filled
        in yet (e.g. still mid-authoring) bridges cleanly to an empty,
        never a stale/wrong, formula -- never falls back to an unrelated
        field like `technical_name`."""
        snapshot = _snapshot(
            concept_type=CatalogConceptType.METRIC,
            concept_key="clv",
            technical_name="SUM(Amount)",  # deliberately NOT the formula source
        )
        definition = metric_definition_from_snapshot(snapshot)
        assert definition is not None
        assert definition.formula == ""
        assert definition.source_tables == ()
        assert definition.valid_filters == ()

    def test_status_mapping_published_to_approved(self):
        snapshot = _snapshot(concept_type=CatalogConceptType.METRIC, status=CatalogStatus.PUBLISHED)
        definition = metric_definition_from_snapshot(snapshot)
        assert definition is not None
        assert definition.status == MetricStatus.APPROVED

    def test_status_mapping_draft_and_reviewed_to_draft(self):
        for status in (CatalogStatus.DRAFT, CatalogStatus.REVIEWED):
            snapshot = _snapshot(concept_type=CatalogConceptType.METRIC, status=status)
            definition = metric_definition_from_snapshot(snapshot)
            assert definition is not None
            assert definition.status == MetricStatus.DRAFT

    def test_status_mapping_superseded_to_deprecated(self):
        snapshot = _snapshot(
            concept_type=CatalogConceptType.METRIC, status=CatalogStatus.SUPERSEDED
        )
        definition = metric_definition_from_snapshot(snapshot)
        assert definition is not None
        assert definition.status == MetricStatus.DEPRECATED


class TestBuildMetricRegistryFromCatalog:
    def test_only_metric_type_entries_are_included(self):
        entries = [
            _snapshot(
                concept_type=CatalogConceptType.METRIC,
                concept_key="clv",
                status=CatalogStatus.PUBLISHED,
            ),
            _snapshot(
                concept_type=CatalogConceptType.ENTITY,
                concept_key="customer",
                status=CatalogStatus.PUBLISHED,
            ),
            _snapshot(
                concept_type=CatalogConceptType.DIMENSION,
                concept_key="region",
                status=CatalogStatus.PUBLISHED,
            ),
        ]
        registry = build_metric_registry_from_catalog(entries)
        all_metrics = registry.list_all()
        assert len(all_metrics) == 1
        assert all_metrics[0].name == "Customer"  # business_name default from _snapshot

    def test_empty_input_produces_an_empty_registry(self):
        registry = build_metric_registry_from_catalog([])
        assert registry.list_all() == ()
        assert registry.get("anything") is None

    def test_get_looks_up_by_business_name(self):
        entries = [
            _snapshot(
                concept_type=CatalogConceptType.METRIC,
                concept_key="clv",
                business_name="Customer Lifetime Value",
                status=CatalogStatus.PUBLISHED,
            )
        ]
        registry = build_metric_registry_from_catalog(entries)
        found = registry.get("Customer Lifetime Value")
        assert found is not None
        assert found.status == MetricStatus.APPROVED
