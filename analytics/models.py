"""Typed shapes for one query result's analytics findings.

Every `AnalyticsFinding` this module's own default provider produces
carries `claim.level == DataTruthLevel.DATABASE_FACT` -- nothing here
involves an LLM; a finding is a typed, human-readable rendering of a
statistic `agent.insight.summarize_result` already computed
deterministically. See `analytics/provider.py` for the adapter that
builds these.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from agent.insight import OutlierStat, TrendStat
from agent.provenance import ProvenancedClaim


class AnalyticsFindingKind(str, Enum):
    """What kind of statistical finding one `AnalyticsFinding` reports.

    A closed set, not a free-form string, mirroring
    `retrieval.models.ChunkType`'s own "deliberately closed" rationale --
    a caller (a future UI trend badge, an eval script) can exhaustively
    switch on this instead of string-matching.
    """

    TREND = "trend"
    OUTLIER = "outlier"
    VARIANCE = "variance"


class AnalyticsFinding(BaseModel):
    """One statistical finding about a query result, already computed and
    already true of the data -- never an LLM's own claim about it.

    Exactly one of `trend`/`outlier`/`variance_column` is set, matching
    `kind`. Modeled as one class with optional per-kind fields (rather than
    three separate finding classes) so `AnalyticsResult.findings` can be a
    single homogeneous tuple a caller iterates without an `isinstance`
    check per element -- `kind` alone is enough to know which field to read.
    """

    model_config = ConfigDict(frozen=True)

    kind: AnalyticsFindingKind
    #: A short, deterministic, human-readable rendering of this finding
    #: (e.g. "Sales rose 12.4% from 2011 to 2014."). Always
    #: `DataTruthLevel.DATABASE_FACT` for this module's own default
    #: provider -- see this module's own docstring.
    claim: ProvenancedClaim
    trend: TrendStat | None = None
    outlier: OutlierStat | None = None
    #: Set only when `kind == VARIANCE` -- the column this finding is about.
    #: Not a full `ColumnStat` (that would duplicate `stddev`/
    #: `coefficient_of_variation`, which already live in `claim.value`'s
    #: rendering) -- just enough to let a caller group variance findings by
    #: column without re-parsing `claim.value`.
    variance_column: str | None = None


class AnalyticsResult(BaseModel):
    """The full set of analytics findings for one query result.

    Attributes:
        row_count: The result's row count, carried alongside `findings` so
            a caller can distinguish "genuinely nothing to find" (e.g. a
            single-row aggregate) from "findings were computed but empty."
        findings: Every finding this result produced -- empty (never a
            sentinel `None`) when there was nothing to report, so a caller
            can always iterate this without a `None` check, the same
            convention `agent.insight.ResultSummary.outliers` already uses.
    """

    model_config = ConfigDict(frozen=True)

    row_count: int
    findings: tuple[AnalyticsFinding, ...] = ()
