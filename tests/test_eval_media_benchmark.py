"""Unit tests for eval/media_benchmark/ -- the benchmark FRAMEWORK's own
logic (dataset loading, grading), not a live run against a real library
(that stays manual, like `scripts/run_media_eval.py` itself -- see
`eval/media_benchmark/__init__.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.media_benchmark.dataset_loader import MediaDatasetValidationError, load_media_dataset
from eval.media_benchmark.runner import run_case
from eval.media_benchmark.schema import MediaBenchmarkCase
from media.search import MediaHit


class TestLoadMediaDataset:
    def test_missing_file_returns_empty_tuple(self, tmp_path: Path):
        assert load_media_dataset(tmp_path / "does-not-exist.yaml") == ()

    def test_empty_cases_list_returns_empty_tuple(self, tmp_path: Path):
        dataset_path = tmp_path / "dataset.yaml"
        dataset_path.write_text("cases: []\n")
        assert load_media_dataset(dataset_path) == ()

    def test_loads_a_valid_case(self, tmp_path: Path):
        dataset_path = tmp_path / "dataset.yaml"
        dataset_path.write_text(
            "cases:\n"
            "  - id: case1\n"
            "    query: find the inspection photo\n"
            "    expected_filename: inspection.jpg\n"
            "    media_type: image\n"
        )
        cases = load_media_dataset(dataset_path)
        assert len(cases) == 1
        assert cases[0].id == "case1"
        assert cases[0].expected_filename == "inspection.jpg"

    def test_missing_required_field_raises(self, tmp_path: Path):
        dataset_path = tmp_path / "dataset.yaml"
        dataset_path.write_text("cases:\n  - id: case1\n    query: x\n")
        with pytest.raises(MediaDatasetValidationError):
            load_media_dataset(dataset_path)

    def test_duplicate_id_raises(self, tmp_path: Path):
        dataset_path = tmp_path / "dataset.yaml"
        dataset_path.write_text(
            "cases:\n"
            "  - id: dup\n    query: a\n    expected_filename: a.jpg\n"
            "  - id: dup\n    query: b\n    expected_filename: b.jpg\n"
        )
        with pytest.raises(MediaDatasetValidationError):
            load_media_dataset(dataset_path)


class TestRunCase:
    def test_correct_top_hit(self, monkeypatch):
        case = MediaBenchmarkCase(
            id="c1", query="the inspection photo", expected_filename="inspection.jpg"
        )
        monkeypatch.setattr(
            "eval.media_benchmark.runner.search_media",
            lambda query, settings, media_type, top_k: [
                MediaHit(media_id="img1", media_type="image", caption="x", similarity=0.9)
            ],
        )
        monkeypatch.setattr(
            "eval.media_benchmark.runner.get_image_metadata",
            lambda media_id, settings: {"source_path": "/lib/inspection.jpg"},
        )

        result = run_case(case, settings=None, top_k=5)

        assert result.top_hit_correct is True
        assert result.hit_at_k is True

    def test_wrong_top_hit_but_present_lower_down_counts_for_hit_at_k_only(self, monkeypatch):
        case = MediaBenchmarkCase(id="c1", query="x", expected_filename="right.jpg")
        monkeypatch.setattr(
            "eval.media_benchmark.runner.search_media",
            lambda query, settings, media_type, top_k: [
                MediaHit(media_id="img1", media_type="image", caption="x", similarity=0.9),
                MediaHit(media_id="img2", media_type="image", caption="x", similarity=0.8),
            ],
        )
        monkeypatch.setattr(
            "eval.media_benchmark.runner.get_image_metadata",
            lambda media_id, settings: {
                "source_path": "/lib/wrong.jpg" if media_id == "img1" else "/lib/right.jpg"
            },
        )

        result = run_case(case, settings=None, top_k=5)

        assert result.top_hit_correct is False
        assert result.hit_at_k is True

    def test_video_case_checks_timestamp_containment(self, monkeypatch):
        case = MediaBenchmarkCase(
            id="c1",
            query="the crane clip",
            expected_filename="site.mp4",
            media_type="video",
            expected_timestamp=12.0,
        )
        monkeypatch.setattr(
            "eval.media_benchmark.runner.search_media",
            lambda query, settings, media_type, top_k: [
                MediaHit(
                    media_id="seg1",
                    media_type="video",
                    caption="x",
                    similarity=0.9,
                    timestamp_start=10.0,
                    timestamp_end=15.0,
                )
            ],
        )
        monkeypatch.setattr(
            "eval.media_benchmark.runner.get_segment_metadata",
            lambda media_id, settings: {"source_path": "/lib/site.mp4"},
        )

        result = run_case(case, settings=None, top_k=5)

        assert result.timestamp_correct is True

    def test_search_failure_is_captured_not_raised(self, monkeypatch):
        case = MediaBenchmarkCase(id="c1", query="x", expected_filename="x.jpg")

        def _raise(query, settings, media_type, top_k):
            raise RuntimeError("chroma unavailable")

        monkeypatch.setattr("eval.media_benchmark.runner.search_media", _raise)

        result = run_case(case, settings=None, top_k=5)

        assert result.error == "chroma unavailable"
        assert result.hit_at_k is False
