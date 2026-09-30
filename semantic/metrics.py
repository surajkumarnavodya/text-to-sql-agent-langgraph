"""`MetricDefinition`, `MetricRegistry`, and the one real default
implementation -- see this package's own `__init__.py` docstring for why
this is a separate, typed module rather than a reuse of
`retrieval.models.ChunkType.METRIC`.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class MetricStatus(str, Enum):
    """A metric definition's governance state.

    Attributes:
        DRAFT: Entered, but not reviewed/approved by a human -- the status
            every entry loaded from `data/knowledge/metrics.yaml` gets
            today, since nothing in this codebase currently represents a
            real human-approval step for a metric definition (disclosed
            explicitly, not silently assumed -- see
            `YamlMetricRegistry`'s own docstring).
        APPROVED: A human has explicitly reviewed and confirmed this
            definition is correct -- the `agent.provenance
            .DataTruthLevel.CONFIRMED_BUSINESS_TRUTH` state, applied to a
            metric. No code path in this codebase sets this today.
        DEPRECATED: Superseded (see `MetricDefinition.supersedes`) or
            otherwise retired -- still resolvable by name for historical
            queries, but should not be offered as a fresh suggestion.
    """

    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class MetricDefinition(BaseModel):
    """One governed business metric's authoritative definition.

    Attributes:
        name: Canonical metric name (e.g. "gross sales amount") -- the
            lookup key `MetricRegistry.get` uses.
        definition: Plain-English definition.
        formula: A SQL-expression-shaped hint (e.g. `"SUM(SalesAmount)"`)
            -- illustrative, not literal executable SQL (mirrors
            `data/knowledge/metrics.yaml`'s own header comment).
        aggregation: The aggregation shape (e.g. `"SUM"`, `"COUNT DISTINCT"`,
            `"derived ratio"`).
        grain: What one row of the aggregation represents.
        valid_filters: Columns commonly used to filter this metric.
        source_tables: Tables this metric is computed from.
        source_columns: `table.column` pairs this metric reads.
        time_period_interpretation: How a "this year"/"last quarter" style
            filter on this metric should be interpreted.
        tags: Free-form labels.
        owner: Who is authoritative for this definition -- `None` today,
            since no existing content in this codebase records one (a
            real gap a future governance-workflow prompt would need to
            close, not invented here).
        status: See `MetricStatus`. Defaults to `DRAFT`.
        version: Governance version -- distinct from, and never conflated
            with, `retrieval.models.Chunk.version` (that field means
            "re-ingestion/idempotency version," an unrelated concept).
        supersedes: The `name` of a prior metric definition this one
            replaces, if any.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    definition: str = ""
    formula: str = ""
    aggregation: str = ""
    grain: str = ""
    valid_filters: tuple[str, ...] = ()
    source_tables: tuple[str, ...] = ()
    source_columns: tuple[str, ...] = ()
    time_period_interpretation: str = ""
    tags: tuple[str, ...] = ()
    owner: str | None = None
    status: MetricStatus = MetricStatus.DRAFT
    version: int = 1
    supersedes: str | None = None


@runtime_checkable
class MetricRegistry(Protocol):
    """A source of governed metric definitions.

    `runtime_checkable`, matching `analytics.provider.AnalyticsProvider`'s
    own convention -- see that module's docstring for why.
    """

    def get(self, name: str) -> MetricDefinition | None:
        """Returns the named metric's current definition, or `None` if no
        metric by that name is registered."""
        ...

    def list_all(self) -> tuple[MetricDefinition, ...]:
        """Returns every registered metric definition."""
        ...


def _load_yaml_metrics(path: Path) -> list[dict[str, Any]]:
    """Generic `metrics:` list loader for `data/knowledge/metrics.yaml`.

    Deliberately a small, separate function from `retrieval.chunking
    ._load_yaml_list` rather than an import of it -- that function is
    module-private (`_`-prefixed) by design, and this codebase's own
    established convention for exactly this situation (see
    `attachments/processors/pdf_processor.py`'s docstring on why it
    duplicates rather than imports `rag/ingestion.py`'s private OCR
    helper) is a small, deliberate duplication of a generic loader rather
    than reaching into another module's private internals. Mirrors that
    function's exact "missing file is not an error, return empty"
    contract, since `data/knowledge/metrics.yaml` is equally optional,
    hand-authored content.
    """
    if not path.exists():
        logger.info("Metrics knowledge file not found, skipping: %s", path)
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records = raw.get("metrics") or []
    if not isinstance(records, list):
        logger.warning("Expected a list under 'metrics' in %s, got %s", path, type(records))
        return []
    return records


class YamlMetricRegistry:
    """The default, and today the only, `MetricRegistry` -- loads
    `data/knowledge/metrics.yaml` (the same file
    `retrieval.chunking.metric_chunks_from_yaml` already parses for
    similarity-based retrieval) and maps each entry to a typed
    `MetricDefinition`.

    Every loaded definition gets `status=MetricStatus.DRAFT` and
    `owner=None` -- disclosed, not invented: no human-approval workflow or
    ownership record exists anywhere in this codebase today (see
    `MetricStatus.DRAFT`'s own docstring). A future governance-workflow
    prompt is the right place to add real `APPROVED`/`owner` data, backed
    by real persistence -- out of scope for this target-architecture
    prompt, which adds typed contracts and a read-only adapter over
    existing content only.

    Loaded once at construction time, not re-read per call -- if the
    underlying YAML changes, construct a new instance. This mirrors
    `config.table_descriptions.load_table_descriptions`'s *opposite*
    choice (re-read fresh every call, so a hand-edit takes effect
    immediately) deliberately not being copied here: unlike a live
    schema-retrieval prompt, nothing reads this registry on a hot path
    yet, so there is no "editors expect their fix to apply immediately"
    requirement to satisfy, and re-parsing YAML on every `get()` call
    would be needless work for a class with no consumer yet.
    """

    def __init__(self, definitions: tuple[MetricDefinition, ...]) -> None:
        self._by_name = {d.name: d for d in definitions}

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> YamlMetricRegistry:
        """Loads from `Settings.retrieval_knowledge_dir / "metrics.yaml"`
        -- the same path `retrieval/ingestion.py` already resolves its own
        metrics loading from, so this and the fuzzy-retrieval path always
        read the same source file."""
        resolved_settings = settings or get_settings()
        path = resolved_settings.retrieval_knowledge_dir / "metrics.yaml"
        definitions = tuple(
            MetricDefinition(
                name=entry["name"],
                definition=entry.get("definition") or "",
                formula=entry.get("formula") or "",
                aggregation=entry.get("aggregation") or "",
                grain=entry.get("grain") or "",
                valid_filters=tuple(entry.get("valid_filters") or ()),
                source_tables=tuple(entry.get("source_tables") or ()),
                source_columns=tuple(entry.get("source_columns") or ()),
                time_period_interpretation=entry.get("time_period_interpretation") or "",
                tags=tuple(entry.get("tags") or ()),
            )
            for entry in _load_yaml_metrics(path)
            if entry.get("name")
        )
        return cls(definitions)

    def get(self, name: str) -> MetricDefinition | None:
        return self._by_name.get(name)

    def list_all(self) -> tuple[MetricDefinition, ...]:
        return tuple(self._by_name.values())
