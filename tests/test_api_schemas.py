"""Unit tests for `api/schemas.py` additions -- Prompt 13
(`13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md`)'s `AskResponse.analytical_result`
field, and Prompt 15 (`15_VISUALIZATION_ENGINE_CONTRACT.md`)'s
`ExecuteResponse.visualization_spec` field. A minimal, dedicated file
since no existing test file targeted `api/schemas.py` directly.
"""

from __future__ import annotations

from typing import Any

from api.schemas import (
    AccessibilityMetadataOut,
    AskResponse,
    ChartFieldOut,
    ExecuteResponse,
    VisualizationSpecOut,
)


def _minimal_response(**overrides: Any) -> AskResponse:
    base: dict[str, Any] = {"session_id": "s1", "status": "succeeded"}
    base.update(overrides)
    return AskResponse(**base)


def _minimal_execute_response(**overrides: Any) -> ExecuteResponse:
    base: dict[str, Any] = {"status": "succeeded", "database": "default"}
    base.update(overrides)
    return ExecuteResponse(**base)


class TestAskResponseAnalyticalResult:
    def test_defaults_to_none(self):
        response = _minimal_response()
        assert response.analytical_result is None

    def test_round_trips_a_plain_dict(self):
        payload = {
            "row_count": 3,
            "findings": [],
            "shape": "categorical_aggregate",
            "engine_version": "1.0.0",
            "insufficient_data_reasons": [],
        }
        response = _minimal_response(analytical_result=payload)
        assert response.analytical_result == payload
        # Confirms it serializes through the response's own JSON mode too
        # (not just held as a raw Python dict reference).
        assert response.model_dump(mode="json")["analytical_result"] == payload

    def test_existing_response_construction_without_the_field_is_unaffected(self):
        """Backward compatibility: every pre-Prompt-13 AskResponse
        construction call (which never passed this field) still works."""
        response = AskResponse(session_id="s2", status="failed", sql=None)
        assert response.analytical_result is None


class TestExecuteResponseVisualizationSpec:
    def test_defaults_to_none(self):
        response = _minimal_execute_response()
        assert response.visualization_spec is None

    def test_round_trips_a_constructed_spec(self):
        spec = VisualizationSpecOut(
            chart_type="bar",
            title="Total Sales by Category",
            fields=(
                ChartFieldOut(column="Category", role="x", aggregation=None, format="plain"),
                ChartFieldOut(column="TotalSales", role="y", aggregation="sum", format="currency"),
            ),
            sort=None,
            limit=20,
            is_downsampled=False,
            notices=(),
            accessibility=AccessibilityMetadataOut(alt_text="alt", summary="summary"),
            reason="A category column with a numeric measure compares cleanly as a bar chart.",
            engine_version="1.0.0",
        )
        response = _minimal_execute_response(visualization_spec=spec)
        assert response.visualization_spec is not None
        assert response.visualization_spec.chart_type == "bar"
        assert response.visualization_spec.fields[1].aggregation == "sum"
        dumped = response.model_dump(mode="json")
        assert dumped["visualization_spec"]["title"] == "Total Sales by Category"

    def test_existing_response_construction_without_the_field_is_unaffected(self):
        """Backward compatibility: every pre-Prompt-15 ExecuteResponse
        construction call (which never passed this field) still works."""
        response = _minimal_execute_response(status="failed", error="boom")
        assert response.visualization_spec is None
