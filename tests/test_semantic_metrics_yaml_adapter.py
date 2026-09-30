"""Wiring test for semantic/metrics.py's `YamlMetricRegistry.from_settings`:
proves it loads the real `data/knowledge/metrics.yaml` this codebase
already ships and ingests via `retrieval/chunking.py::metric_chunks_from_yaml`
-- the same file, not a duplicate copy -- and maps it to typed
`MetricDefinition`s correctly.
"""

from __future__ import annotations

from semantic.metrics import MetricStatus, YamlMetricRegistry

from config.settings import Settings


class TestYamlMetricRegistryFromSettings:
    def test_loads_the_real_metrics_yaml(self):
        registry = YamlMetricRegistry.from_settings(Settings())
        # data/knowledge/metrics.yaml ships 6 real, named metrics as of
        # this writing (see that file's own header comment) -- asserting
        # "at least" rather than an exact count so adding a 7th metric to
        # the file later doesn't break this test.
        assert len(registry.list_all()) >= 6

    def test_a_real_known_metric_maps_correctly(self):
        registry = YamlMetricRegistry.from_settings(Settings())
        metric = registry.get("gross sales amount")
        assert metric is not None
        assert metric.formula == "SUM(SalesAmount)"
        assert metric.aggregation == "SUM"
        assert "FactInternetSales" in metric.source_tables
        assert "FactResellerSales" in metric.source_tables
        assert "sales" in metric.tags

    def test_every_loaded_metric_defaults_to_draft_with_no_owner(self):
        """Disclosed, not silently assumed: no human-approval workflow
        exists anywhere in this codebase today -- see
        semantic/metrics.py's YamlMetricRegistry docstring."""
        registry = YamlMetricRegistry.from_settings(Settings())
        for metric in registry.list_all():
            assert metric.status == MetricStatus.DRAFT
            assert metric.owner is None

    def test_missing_file_returns_an_empty_registry(self, tmp_path):
        settings = Settings(retrieval_knowledge_dir=tmp_path / "does-not-exist")
        registry = YamlMetricRegistry.from_settings(settings)
        assert registry.list_all() == ()
