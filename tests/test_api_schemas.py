"""Unit tests for `api/schemas.py` additions -- Prompt 13
(`13_ANALYTICAL_RESULT_ENGINE_CONTRACT.md`)'s `AskResponse.analytical_result`
field. A minimal, dedicated file since no existing test file targeted
`api/schemas.py` directly.
"""

from __future__ import annotations

from typing import Any

from api.schemas import AskResponse


def _minimal_response(**overrides: Any) -> AskResponse:
    base: dict[str, Any] = {"session_id": "s1", "status": "succeeded"}
    base.update(overrides)
    return AskResponse(**base)


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
