"""Isolated contract tests for semantic/metrics.py -- hand-built
`MetricDefinition`s, no real YAML file involved (see
tests/test_semantic_metrics_yaml_adapter.py for that)."""

from __future__ import annotations

from semantic.metrics import MetricDefinition, MetricRegistry, MetricStatus, YamlMetricRegistry


class TestMetricDefinition:
    def test_defaults_to_draft_status_and_no_owner(self):
        metric = MetricDefinition(name="test metric")
        assert metric.status == MetricStatus.DRAFT
        assert metric.owner is None
        assert metric.version == 1
        assert metric.supersedes is None

    def test_tuple_fields_default_to_empty(self):
        metric = MetricDefinition(name="test metric")
        assert metric.valid_filters == ()
        assert metric.source_tables == ()
        assert metric.source_columns == ()
        assert metric.tags == ()


class TestYamlMetricRegistrySatisfiesProtocol:
    def test_isinstance_check(self):
        registry = YamlMetricRegistry(definitions=())
        assert isinstance(registry, MetricRegistry)


class TestYamlMetricRegistryHandBuilt:
    def test_get_returns_none_for_unknown_metric(self):
        registry = YamlMetricRegistry(definitions=())
        assert registry.get("nonexistent metric") is None

    def test_get_returns_the_matching_definition(self):
        metric = MetricDefinition(name="gross margin", formula="SUM(a) - SUM(b)")
        registry = YamlMetricRegistry(definitions=(metric,))
        assert registry.get("gross margin") == metric

    def test_list_all_returns_every_definition(self):
        metric_a = MetricDefinition(name="metric a")
        metric_b = MetricDefinition(name="metric b")
        registry = YamlMetricRegistry(definitions=(metric_a, metric_b))
        assert set(registry.list_all()) == {metric_a, metric_b}
