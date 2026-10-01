"""Deterministic visualization specification builder -- Prompt 15
(`15_VISUALIZATION_ENGINE_CONTRACT.md`).

`build_chart_spec` turns a raw SQL result (`columns`, `rows`, and the
already-computed `column_types` from `agent.result_charting
.classify_columns`) into a full `ChartSpec`: chart type, fields/roles,
an inferred aggregation/unit per field, a title, a default sort, a
deterministic top-N limit, and accessibility metadata. Pure,
deterministic, no LLM, no I/O -- the same posture every other module
under `analytics/` already establishes.

**Deliberately additive to, never a replacement for,
`agent.result_charting.classify_columns`/`recommend_chart`** -- those
stay completely untouched (`ExecuteResponse.chart_recommendation`/
`column_types` keep powering `frontend/src/lib/chartEngine.ts`'s own
seed-selection exactly as before). This module takes `column_types` as
an input rather than re-inferring it, so there is exactly one
numeric/date/text classification in this codebase, not two.

**Never recommends a chart type the frontend cannot actually render.**
`frontend/src/lib/chartEngine.ts` has no box-and-whisker or map
rendering capability (and this module deliberately does not add a new
charting library/plugin to get one -- see this prompt's own "do not
introduce duplicate chart libraries" requirement). A single-numeric-
column distribution is recommended as `HISTOGRAM` (a true histogram is
just a bar chart of binned counts -- genuinely implemented in
`chartEngine.ts` by this same prompt, zero new dependencies);
geography-shaped data falls back to `BAR` (the region name is still a
perfectly good category axis). `ChartType.BOX`/`.MAP` are real,
detected concepts in this module's own taxonomy -- gated behind
`Settings.enable_box_plot_charts`/`enable_map_charts` (both `False` by
default, documenting "not yet renderable in this build") -- so a future
prompt that adds a real box-plot/map rendering capability has a clean,
already-tested backend classification to flip on, rather than dead code
today.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum

from pydantic import BaseModel, ConfigDict

from analytics.models import AnalyticsResult, ForecastResult
from config.settings import Settings, get_settings

#: Bumped when this module's chart-type-selection rules, title/format/
#: aggregation inference, or accessibility-text generation change in a
#: way a consumer should be able to detect -- Prompt 13/14's own "record
#: ... version metadata" convention (master-contract rule 11).
VISUALIZATION_ENGINE_VERSION = "1.0.0"

#: A distribution needs at least this many points for a histogram's
#: bucketing to be meaningful -- fewer than this, a table is the honest
#: default (mirrors `frontend/src/lib/chartEngine.ts`'s own identical
#: minimum for its histogram validity rule).
_MIN_ROWS_FOR_HISTOGRAM = 5


class ChartType(str, Enum):
    """The full chart-type taxonomy -- a superset of `frontend/src/lib
    /chartEngine.ts`'s own `ChartTypeId` (same string values, so a
    future wiring pass needs no translation layer). `BOX`/`MAP` are
    real, detected values this module's own selection logic never
    actually *recommends* while their gating flag is off -- see this
    module's own docstring for why."""

    KPI = "kpi"
    BAR = "bar"
    BAR_HORIZONTAL = "bar-horizontal"
    BAR_STACKED = "bar-stacked"
    LINE = "line"
    AREA = "area"
    PIE = "pie"
    DOUGHNUT = "doughnut"
    SCATTER = "scatter"
    MIXED = "mixed"
    HISTOGRAM = "histogram"
    BOX = "box"
    MAP = "map"
    TABLE = "table"


class FieldRole(str, Enum):
    """What one `ChartField` is used for within a `ChartSpec`."""

    X = "x"
    Y = "y"
    SERIES = "series"
    GROUP = "group"
    GEO_REGION = "geo_region"
    GEO_LATITUDE = "geo_latitude"
    GEO_LONGITUDE = "geo_longitude"


class ChartField(BaseModel):
    """One column used by a `ChartSpec`, with its role and inferred
    descriptive metadata.

    `aggregation`/`format` are **descriptive, best-effort inferences
    from the column's own name** (e.g. a `"TotalRevenue"` alias implies
    `aggregation="sum"`, `format="currency"`) -- never a computation this
    module performs itself. The SQL that produced these rows already did
    any real aggregation; this is purely for building an honest
    title/accessibility description.
    """

    model_config = ConfigDict(frozen=True)

    column: str
    role: FieldRole
    aggregation: str | None = None
    format: str = "plain"


class ChartSort(BaseModel):
    model_config = ConfigDict(frozen=True)

    column: str
    direction: str


class AccessibilityMetadata(BaseModel):
    """A chart's own accessible description -- always non-empty (never a
    bare chart with no usable description is "stable" enough to render).
    """

    model_config = ConfigDict(frozen=True)

    alt_text: str
    summary: str


class ChartSpec(BaseModel):
    """The full deterministic visualization specification for one query
    result -- Prompt 15's own named requirement: "chart type, fields,
    series, aggregation, title, units, sort, limit, formatting and
    accessibility metadata." Never produced for a result shape nothing
    in `chart_type`'s own taxonomy can actually render -- see this
    module's own docstring.
    """

    model_config = ConfigDict(frozen=True)

    chart_type: ChartType
    title: str
    fields: tuple[ChartField, ...]
    sort: ChartSort | None = None
    limit: int | None = None
    is_downsampled: bool = False
    notices: tuple[str, ...] = ()
    accessibility: AccessibilityMetadata
    reason: str
    engine_version: str = VISUALIZATION_ENGINE_VERSION


# ---------------------------------------------------------------------------
# Name tokenization -- shared by geography detection, aggregation
# inference, format inference, and title generation, so all four agree on
# what a column's own name "means" rather than four slightly-drifting
# heuristics.
# ---------------------------------------------------------------------------

#: Splits a camelCase/PascalCase/snake_case/kebab-case identifier into
#: lowercase word tokens (e.g. "EnglishProductCategoryName" ->
#: ["english", "product", "category", "name"]; "total_revenue" ->
#: ["total", "revenue"]) -- a real-world necessity, not an academic one:
#: this app's own schemas (e.g. AdventureWorksDW2025's
#: "SalesTerritoryRegion"/"CountryRegionCode") are camelCase, where a
#: plain `\b`-word-boundary regex would never match "region" or
#: "country" at all (no boundary exists mid-camelCase).
_TOKEN_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def _name_tokens(name: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(name)]


def _humanize(name: str) -> str:
    """Renders a column name as a human-readable title fragment (e.g.
    "EnglishProductCategoryName" -> "English Product Category Name")."""
    return " ".join(token.capitalize() for token in _name_tokens(name)) or name


# ---------------------------------------------------------------------------
# Geography detection -- a small, explicitly disclosed, non-exhaustive
# name-pattern list (the same honesty convention
# `db/relationship_inference.py`'s own heuristic lists already establish
# for this codebase -- a name not matched here simply isn't classified as
# geography, never a false positive from guessing too aggressively).
# ---------------------------------------------------------------------------

_GEO_REGION_WORDS = frozenset(
    {"country", "countries", "state", "province", "region", "city", "territory"}
)
_GEO_LATITUDE_WORDS = frozenset({"lat", "latitude"})
_GEO_LONGITUDE_WORDS = frozenset({"lon", "lng", "longitude"})


def detect_geography_columns(columns: list[str]) -> dict[str, FieldRole]:
    """Tags every column whose name matches a recognized geography
    pattern with a `FieldRole` -- `GEO_REGION` for a country/state/
    province/region/city/territory-shaped name, `GEO_LATITUDE`/
    `GEO_LONGITUDE` for a latitude/longitude-shaped one.

    Runs regardless of whether `Settings.enable_map_charts` is on --
    the classification itself is always real and always computed; only
    the *chart type recommendation* is gated (see `build_chart_spec`).
    """
    roles: dict[str, FieldRole] = {}
    for column in columns:
        tokens = set(_name_tokens(column))
        if tokens & _GEO_LATITUDE_WORDS:
            roles[column] = FieldRole.GEO_LATITUDE
        elif tokens & _GEO_LONGITUDE_WORDS:
            roles[column] = FieldRole.GEO_LONGITUDE
        elif tokens & _GEO_REGION_WORDS:
            roles[column] = FieldRole.GEO_REGION
    return roles


# ---------------------------------------------------------------------------
# Aggregation / unit inference -- descriptive only, see ChartField's own
# docstring for why this never re-aggregates anything itself.
# ---------------------------------------------------------------------------

_AGG_SUM_WORDS = frozenset({"sum", "total"})
_AGG_AVG_WORDS = frozenset({"avg", "average", "mean"})
_AGG_MIN_WORDS = frozenset({"min", "minimum"})
_AGG_MAX_WORDS = frozenset({"max", "maximum"})
_AGG_COUNT_WORDS = frozenset({"count", "qty", "quantity", "num", "number"})

_CURRENCY_WORDS = frozenset(
    {"amount", "revenue", "price", "cost", "sales", "spend", "income", "salary", "fee", "value"}
)
_PERCENT_WORDS = frozenset({"percent", "percentage", "pct", "rate", "ratio", "share"})


def _infer_aggregation(column_name: str) -> str | None:
    tokens = set(_name_tokens(column_name))
    if tokens & _AGG_COUNT_WORDS:
        return "count"
    if tokens & _AGG_SUM_WORDS:
        return "sum"
    if tokens & _AGG_AVG_WORDS:
        return "avg"
    if tokens & _AGG_MIN_WORDS:
        return "min"
    if tokens & _AGG_MAX_WORDS:
        return "max"
    return None


def _infer_format(column_name: str) -> str:
    tokens = set(_name_tokens(column_name))
    if tokens & _PERCENT_WORDS:
        return "percent"
    if tokens & _CURRENCY_WORDS:
        return "currency"
    return "plain"


_AGGREGATION_TITLE_PREFIX = {
    "sum": "Total ",
    "avg": "Average ",
    "min": "Minimum ",
    "max": "Maximum ",
    "count": "",
}


def _label_with_aggregation_prefix(field: ChartField) -> str:
    """Renders a field's humanized name, prefixed with its inferred
    aggregation (e.g. "Total Revenue") -- unless the column's own name
    already states it (e.g. "TotalSales" humanizes to "Total Sales",
    which already reads correctly; prefixing again would produce "Total
    Total Sales"). `_infer_aggregation`/`_humanize` share the same
    tokenizer, so this check is exact, not a fragile string match.
    """
    label = _humanize(field.column)
    prefix = _AGGREGATION_TITLE_PREFIX.get(field.aggregation or "", "")
    if not prefix:
        return label
    already_stated = prefix.strip().lower() in set(_name_tokens(field.column))
    return label if already_stated else f"{prefix}{label}"


def _build_title(
    chart_type: ChartType, x_field: ChartField | None, y_fields: list[ChartField]
) -> str:
    if not y_fields:
        return "Chart"
    primary = y_fields[0]
    if chart_type == ChartType.SCATTER and x_field is not None:
        return f"{_humanize(primary.column)} vs {_humanize(x_field.column)}"
    if chart_type == ChartType.HISTOGRAM:
        return f"Distribution of {_humanize(primary.column)}"
    y_label = _label_with_aggregation_prefix(primary)
    if chart_type in (ChartType.KPI, ChartType.BOX):
        return y_label
    if x_field is not None:
        return f"{y_label} by {_humanize(x_field.column)}"
    return y_label


# ---------------------------------------------------------------------------
# Accessibility text
# ---------------------------------------------------------------------------

#: Finding kinds (see `analytics.models.AnalyticsFindingKind`) whose
#: claim text is grounded, human-readable, and relevant enough to reuse
#: directly as a chart's own accessible description -- checked in this
#: priority order.
_ACCESSIBILITY_FINDING_KINDS = ("ranking", "growth", "column_summary")


def _build_accessibility(
    chart_type: ChartType,
    title: str,
    row_count: int,
    analytics_result: AnalyticsResult | None,
) -> AccessibilityMetadata:
    """Reuses an already-computed `AnalyticsResult`'s own grounded claim
    text (Prompt 13/14, `DataTruthLevel.DATABASE_FACT`) as the chart's
    accessible description when one is available -- never re-derives a
    description from scratch when this codebase already computed one.
    Falls back to a generic, deterministic description otherwise; never
    empty either way.
    """
    if analytics_result is not None:
        findings_by_kind = {f.kind.value: f for f in analytics_result.findings}
        for kind in _ACCESSIBILITY_FINDING_KINDS:
            finding = findings_by_kind.get(kind)
            if finding is not None:
                text = f"{title}. {finding.claim.value}"
                return AccessibilityMetadata(alt_text=text, summary=finding.claim.value)

    chart_label = chart_type.value.replace("-", " ")
    text = f"{chart_label.capitalize()} chart titled '{title}' with {row_count} data point(s)."
    return AccessibilityMetadata(alt_text=text, summary=text)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_chart_spec(
    columns: list[str],
    rows: list[tuple],
    column_types: Mapping[str, str],
    analytics_result: AnalyticsResult | None = None,
    settings: Settings | None = None,
) -> ChartSpec | None:
    """Builds a full `ChartSpec` for `columns`/`rows`, or `None` when no
    chart type in this module's own renderable taxonomy fits (the honest
    "table only" default -- the identical contract
    `agent.result_charting.recommend_chart` already establishes).

    Args:
        columns: Column names, in result order.
        rows: Result rows.
        column_types: Per-column `"numeric"`/`"date"`/`"text"`
            classification -- `agent.result_charting.classify_columns`'s
            own output, reused directly (never re-inferred here).
        analytics_result: An already-computed `analytics.engine
            .AnalyticsResult` for this same `columns`/`rows` (e.g. from a
            fresh `compute_analytics_result` call at the same call
            site), used to ground the accessibility text in a real,
            already-computed finding when available. `None` falls back
            to a generic description.
        settings: Defaults to `config.settings.get_settings()`.
    """
    settings = settings or get_settings()
    row_count = len(rows)
    if row_count == 0 or not columns:
        return None

    numeric_cols = [c for c in columns if column_types.get(c) == "numeric"]
    date_cols = [c for c in columns if column_types.get(c) == "date"]
    text_cols = [c for c in columns if column_types.get(c) == "text"]
    geo_roles = detect_geography_columns(columns)

    chart_type: ChartType
    reason: str
    x_column: str | None = None
    y_columns: list[str]

    if not numeric_cols:
        return None

    if row_count == 1:
        chart_type = ChartType.KPI
        reason = "A single row reads most clearly as a stat card."
        y_columns = numeric_cols[:1]
    elif date_cols:
        chart_type = ChartType.LINE
        reason = "A date column with a numeric measure shows a trend over time."
        x_column = date_cols[0]
        y_columns = numeric_cols
    elif text_cols:
        geo_text = next((c for c in text_cols if geo_roles.get(c) == FieldRole.GEO_REGION), None)
        if geo_text is not None and settings.enable_map_charts:
            chart_type = ChartType.MAP
            reason = "A geography-shaped category column suits a map."
            x_column = geo_text
        else:
            chart_type = ChartType.BAR
            reason = "A category column with a numeric measure compares cleanly as a bar chart."
            x_column = text_cols[0]
        y_columns = numeric_cols
    elif len(numeric_cols) == 2:
        lat = next((c for c in numeric_cols if geo_roles.get(c) == FieldRole.GEO_LATITUDE), None)
        lon = next((c for c in numeric_cols if geo_roles.get(c) == FieldRole.GEO_LONGITUDE), None)
        if lat and lon and settings.enable_map_charts:
            chart_type = ChartType.MAP
            reason = "Latitude/longitude columns suit a map."
            x_column = lon
            y_columns = [lat]
        else:
            chart_type = ChartType.SCATTER
            reason = "Two numeric measures can reveal a relationship as a scatter plot."
            x_column = numeric_cols[0]
            y_columns = [numeric_cols[1]]
    elif len(numeric_cols) == 1:
        if row_count < _MIN_ROWS_FOR_HISTOGRAM:
            return None
        if settings.enable_box_plot_charts:
            chart_type = ChartType.BOX
            reason = "A single numeric column's spread suits a box plot."
        else:
            chart_type = ChartType.HISTOGRAM
            reason = "A single numeric column's distribution suits a histogram."
        y_columns = numeric_cols
    else:
        return None

    fields: list[ChartField] = []
    x_field: ChartField | None = None
    if x_column is not None:
        role = geo_roles.get(x_column, FieldRole.X)
        x_field = ChartField(
            column=x_column,
            role=role,
            aggregation=None,
            format=_infer_format(x_column),
        )
        fields.append(x_field)
    y_fields = [
        ChartField(
            column=col,
            role=FieldRole.Y,
            aggregation=_infer_aggregation(col),
            format=_infer_format(col),
        )
        for col in y_columns
    ]
    fields.extend(y_fields)

    title = _build_title(chart_type, x_field, y_fields)

    sort: ChartSort | None = None
    if chart_type == ChartType.LINE:
        sort = ChartSort(column=x_column or "", direction="asc")
    elif chart_type in (ChartType.BAR, ChartType.MAP) and y_fields:
        sort = ChartSort(column=y_fields[0].column, direction="desc")

    limit: int | None = None
    is_downsampled = False
    notices: list[str] = []
    if chart_type in (ChartType.BAR, ChartType.LINE, ChartType.MAP):
        limit = settings.visualization_default_top_n
        if row_count > limit:
            is_downsampled = True
            notices.append(f"Showing top {limit} of {row_count} rows.")

    accessibility = _build_accessibility(chart_type, title, row_count, analytics_result)

    return ChartSpec(
        chart_type=chart_type,
        title=title,
        fields=tuple(fields),
        sort=sort,
        limit=limit,
        is_downsampled=is_downsampled,
        notices=tuple(notices),
        accessibility=accessibility,
        reason=reason,
        engine_version=VISUALIZATION_ENGINE_VERSION,
    )


def build_forecast_chart_spec(
    forecast_result: ForecastResult,
    historical_points: list[tuple[str, float]],
    value_column_name: str = "value",
    settings: Settings | None = None,
) -> ChartSpec | None:
    """Builds a deterministic chart specification for an already-computed
    `analytics.forecasting.ForecastResult` -- Prompt 16's own "integrate
    with the visualization contract" requirement.

    Reuses the exact same `ChartSpec`/`ChartField`/`FieldRole`/
    `AccessibilityMetadata` typed contracts `build_chart_spec` above
    already establishes -- no new chart-data shape. Returns `None` when
    `forecast_result.status != "ok"` (nothing to chart), mirroring
    `build_chart_spec`'s own "no chart type fits -> None" contract.

    Always a `LINE` chart with two distinct `Y`/`SERIES` fields (the
    historical actual series and the forecasted estimate) -- never one
    blended, undifferentiated series -- and discloses the estimate nature
    of the forecasted portion via both `notices` (every one of
    `forecast_result.limitations`, plus which periods are estimates) and
    `accessibility` (`forecast_result.summary`, which itself always says
    "estimate"/"forecast", never phrased as a confirmed value). Not
    consumed by `frontend/src/lib/chartEngine.ts` in this pass -- the
    identical "computed + tested + exposed via the API, not yet wired into
    the frontend" deferral `build_chart_spec`'s own `visualization_spec`
    already established for Prompt 15.

    Args:
        forecast_result: An already-computed `analytics.forecasting
            .generate_forecast` result.
        historical_points: The same `(period, value)` series that was fed
            into `generate_forecast` -- used only to anchor the "estimates
            start after period X" notice; never re-validated or
            re-forecast here.
        value_column_name: The historical series' own value column name
            (e.g. `"TotalRevenue"`), used for titling/format inference via
            the exact same `_humanize`/`_infer_format` helpers
            `build_chart_spec` uses. Defaults to a generic `"value"` when
            the caller doesn't have a more specific column name on hand.
        settings: Defaults to `config.settings.get_settings()`.
    """
    settings = settings or get_settings()
    if forecast_result.status != "ok" or forecast_result.model is None:
        return None

    x_field = ChartField(column="period", role=FieldRole.X, aggregation=None, format="plain")
    value_format = _infer_format(value_column_name)
    actual_field = ChartField(
        column=value_column_name, role=FieldRole.Y, aggregation=None, format=value_format
    )
    forecast_field = ChartField(
        column=f"{value_column_name}_forecast",
        role=FieldRole.SERIES,
        aggregation=None,
        format=value_format,
    )

    model_name = forecast_result.model.model.value
    title = f"{_humanize(value_column_name)} Forecast ({model_name.replace('_', ' ').title()})"

    notices: list[str] = []
    if historical_points:
        notices.append(
            f"Periods after {historical_points[-1][0]} are forecasted estimates, not actual "
            "results."
        )
    notices.extend(forecast_result.limitations)

    accessibility = AccessibilityMetadata(
        alt_text=f"{title}. {forecast_result.summary}",
        summary=forecast_result.summary,
    )

    return ChartSpec(
        chart_type=ChartType.LINE,
        title=title,
        fields=(x_field, actual_field, forecast_field),
        sort=ChartSort(column="period", direction="asc"),
        limit=None,
        is_downsampled=False,
        notices=tuple(notices),
        accessibility=accessibility,
        reason="A forecast extends a historical trend line with an estimated continuation.",
        engine_version=VISUALIZATION_ENGINE_VERSION,
    )
