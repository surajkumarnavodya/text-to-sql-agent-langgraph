"""Unit tests for attachments/graph.py -- the attachment-QA LangGraph
subgraph. Mocks the LLM boundary (rag.llm.call_ollama /
attachments.vision.describe_images) so these run fast and offline, matching
tests/test_rag_graph.py's own mocked-LLM style for a comparable subgraph."""

from __future__ import annotations

from unittest.mock import patch

from agent.exceptions import OllamaUnavailableError
from attachments.graph import build_attachment_context_node, run_attachment_qa
from attachments.pipeline import validate_and_store_upload
from attachments.store import AttachmentStore
from config.settings import Settings


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(_env_file=None, attachment_storage_dir=tmp_path, **overrides)


class TestRunAttachmentQa:
    def test_no_attachment_ids_short_circuits(self, tmp_path):
        result = run_attachment_qa("what's in the file?", [])
        assert result["status"] == "no_attachments"
        assert result["answer"] is None

    def test_missing_attachment_id_reports_not_found(self, tmp_path):
        result = run_attachment_qa("what's in the file?", ["ghost-id"])
        assert result["status"] == "failed"
        assert any(e["code"] == "ATTACHMENT_NOT_FOUND" for e in result["errors"])

    def test_happy_path_answers_from_document_text(self, tmp_path):
        store = AttachmentStore()
        settings = _settings(tmp_path)
        attachment, _ = validate_and_store_upload(
            "report.txt",
            "text/plain",
            b"Total Q3 revenue was 500000 dollars.",
            settings,
            owner_subject="alice",
            store=store,
        )

        with (
            patch("attachments.graph.get_default_attachment_store", return_value=store),
            patch("attachments.graph.get_settings", return_value=settings),
            patch(
                "rag.llm.call_ollama",
                return_value="Q3 revenue was 500000 dollars, per report.txt.",
            ),
        ):
            result = run_attachment_qa(
                "what was Q3 revenue?", [attachment.attachment_id], owner_subject="alice"
            )

        assert result["status"] == "succeeded"
        assert "500000" in result["answer"]
        assert result["used_attachment_ids"] == [attachment.attachment_id]

    def test_text_model_answering_empty_gets_a_distinct_message(self, tmp_path):
        """Distinct from the Ollama-unreachable case above: here Ollama
        answers, just with nothing usable -- a different, non-retryable-
        the-same-way failure mode."""
        store = AttachmentStore()
        settings = _settings(tmp_path)
        attachment, _ = validate_and_store_upload(
            "report.txt",
            "text/plain",
            b"irrelevant content",
            settings,
            owner_subject="alice",
            store=store,
        )

        with (
            patch("attachments.graph.get_default_attachment_store", return_value=store),
            patch("attachments.graph.get_settings", return_value=settings),
            patch("rag.llm.call_ollama", return_value=""),
        ):
            result = run_attachment_qa(
                "what was Q3 revenue?", [attachment.attachment_id], owner_subject="alice"
            )

        assert result["status"] == "failed"
        assert result["model_call_outcome"] == "text_llm_empty"
        assert "did not return a usable answer" in result["answer"]

    def test_ownership_mismatch_is_treated_as_not_found(self, tmp_path):
        store = AttachmentStore()
        settings = _settings(tmp_path)
        attachment, _ = validate_and_store_upload(
            "secret.txt",
            "text/plain",
            b"confidential",
            settings,
            owner_subject="alice",
            store=store,
        )

        with (patch("attachments.graph.get_default_attachment_store", return_value=store),):
            result = run_attachment_qa(
                "what does it say?", [attachment.attachment_id], owner_subject="bob"
            )

        assert result["status"] == "failed"
        assert any(e["code"] == "ATTACHMENT_NOT_FOUND" for e in result["errors"])

    def test_ollama_outage_degrades_gracefully_without_crashing(self, tmp_path):
        store = AttachmentStore()
        settings = _settings(tmp_path)
        attachment, _ = validate_and_store_upload(
            "report.txt",
            "text/plain",
            b"some content",
            settings,
            owner_subject="alice",
            store=store,
        )

        with (
            patch("attachments.graph.get_default_attachment_store", return_value=store),
            patch("attachments.graph.get_settings", return_value=settings),
            patch("rag.llm.call_ollama", side_effect=OllamaUnavailableError("down")),
        ):
            result = run_attachment_qa(
                "summarize this", [attachment.attachment_id], owner_subject="alice"
            )

        assert result["status"] == "failed"
        assert result["answer"]  # a clean fallback message, not a crash
        # Differentiated from the "vision never configured" message below --
        # this is "the model was unreachable," a different fix (restart
        # Ollama) from "configure MEDIA_VISION_MODEL."
        assert "unreachable" in result["answer"].lower()
        assert "MEDIA_VISION_MODEL" not in result["answer"]

    def test_vision_model_configured_but_returns_empty_gets_a_distinct_message(self, tmp_path):
        """Distinct from `test_image_with_no_vision_model_falls_back_to_ocr`
        below: here a vision model IS configured and IS actually called
        with real image bytes -- it just comes back empty. The failure
        message must say so, never claim "not configured" for a model that
        was genuinely invoked."""
        import io

        from PIL import Image

        store = AttachmentStore()
        settings = _settings(tmp_path, media_vision_model="qwen3.8:27b")

        buffer = io.BytesIO()
        Image.new("RGB", (100, 100), color="white").save(buffer, format="PNG")
        attachment, _ = validate_and_store_upload(
            "photo.png",
            "image/png",
            buffer.getvalue(),
            settings,
            owner_subject="alice",
            store=store,
        )

        with (
            patch("attachments.graph.get_default_attachment_store", return_value=store),
            patch("attachments.graph.get_settings", return_value=settings),
            # Patched where attachments.graph imported the name (`from
            # attachments.vision import describe_images`), not at its
            # origin module -- patching the latter alone leaves
            # attachments.graph's own already-bound reference untouched,
            # which would make this test issue a real, slow live Ollama
            # call instead of a fast, deterministic mocked one.
            patch("attachments.graph.describe_images", return_value=None),
        ):
            result = run_attachment_qa(
                "what's in this image?", [attachment.attachment_id], owner_subject="alice"
            )

        assert result["status"] == "failed"
        assert result["vision_unavailable"] is False
        assert result["model_call_outcome"] == "vision_empty"
        assert "did not return a usable answer" in result["answer"]
        assert "not configured" not in result["answer"].lower()

    def test_image_with_no_vision_model_falls_back_to_ocr(self, tmp_path):
        import io

        from PIL import Image

        store = AttachmentStore()
        settings = _settings(tmp_path, media_vision_model="")  # vision NOT configured

        buffer = io.BytesIO()
        Image.new("RGB", (100, 100), color="white").save(buffer, format="PNG")
        attachment, _ = validate_and_store_upload(
            "photo.png",
            "image/png",
            buffer.getvalue(),
            settings,
            owner_subject="alice",
            store=store,
        )
        assert attachment.processing_status == "succeeded"
        assert attachment.image_data_url is not None

        with (
            patch("attachments.graph.get_default_attachment_store", return_value=store),
            patch("attachments.graph.get_settings", return_value=settings),
            patch("attachments.graph.extract_text", return_value=""),  # no on-screen text
        ):
            result = run_attachment_qa(
                "what's in this image?", [attachment.attachment_id], owner_subject="alice"
            )

        # No vision model, no OCR text, no other context -> nothing usable,
        # but this must never crash or silently claim a description.
        assert result["vision_unavailable"] is True
        assert result["image_data_urls"] == []
        assert result["status"] == "failed"
        # A real, reported bug: this used to be the same generic "couldn't
        # extract usable information" string regardless of cause. Now it
        # must name the actual fix (configure MEDIA_VISION_MODEL), not a
        # vague catch-all -- see attachments.graph._describe_failure.
        assert "MEDIA_VISION_MODEL" in result["answer"]
        assert result["errors"][-1]["code"] == "MODEL_NO_IMAGE_SUPPORT"


class TestBuildAttachmentContextNode:
    """`build_attachment_context_node` normally excludes an image's
    extracted_text (empty by default -- see `attachments.processors
    .image_processor`'s own docstring) so it never dilutes the text budget.
    The explicit `POST /attachments/{id}/extract-text` action
    (`api/attachments.py`) is the one case that persists real text onto an
    image attachment, and a follow-up question needs to see it -- this is
    what closed that gap (previously an image was excluded unconditionally,
    regardless of whether it carried OCR'd text)."""

    def test_image_without_extracted_text_is_excluded(self, tmp_path):
        settings = _settings(tmp_path)
        state = {
            "processed_attachments": [
                {
                    "attachment_id": "att_1",
                    "filename": "photo.png",
                    "source_type": "image",
                    "extracted_text": "",
                    "metadata": {},
                    "processing_status": "succeeded",
                }
            ]
        }
        with patch("attachments.graph.get_settings", return_value=settings):
            result = build_attachment_context_node(state)
        assert result["attachment_context"] == ""

    def test_image_with_ocr_extracted_text_is_included(self, tmp_path):
        settings = _settings(tmp_path)
        state = {
            "processed_attachments": [
                {
                    "attachment_id": "att_1",
                    "filename": "receipt.png",
                    "source_type": "image",
                    "extracted_text": "Total: $42.00",
                    "metadata": {},
                    "processing_status": "succeeded",
                }
            ]
        }
        with patch("attachments.graph.get_settings", return_value=settings):
            result = build_attachment_context_node(state)
        assert "Total: $42.00" in result["attachment_context"]
        assert "receipt.png" in result["attachment_context"]

    def test_document_attachments_are_unaffected(self, tmp_path):
        settings = _settings(tmp_path)
        state = {
            "processed_attachments": [
                {
                    "attachment_id": "att_1",
                    "filename": "notes.txt",
                    "source_type": "text",
                    "extracted_text": "hello world",
                    "metadata": {},
                    "processing_status": "succeeded",
                }
            ]
        }
        with patch("attachments.graph.get_settings", return_value=settings):
            result = build_attachment_context_node(state)
        assert "hello world" in result["attachment_context"]
