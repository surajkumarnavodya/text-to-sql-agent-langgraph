"""Unit tests for the /media route (api/media.py) -- serves generated
image/video bytes from the in-process `media_gen.cache.MediaCache`, never
the provider's raw CDN URL. Mirrors `tests/test_api_documents.py`'s
`TestDownloadDocument` pattern; no real IMA call is ever involved, since
this route only ever reads back whatever `generation_node` already put in
the cache.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from media_gen.cache import get_media_cache


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


class TestGetMedia:
    def test_returns_bytes_and_content_type_for_a_cached_entry(self, client):
        media_id = get_media_cache().put(b"\x89PNG raw bytes", "image/png")

        response = client.get(f"/media/{media_id}")

        assert response.status_code == 200
        assert response.content == b"\x89PNG raw bytes"
        assert response.headers["content-type"] == "image/png"

    def test_unknown_id_returns_404(self, client):
        response = client.get("/media/does-not-exist")

        assert response.status_code == 404
