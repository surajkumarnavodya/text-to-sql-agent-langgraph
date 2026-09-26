"""Unit tests for attachments/capabilities.py -- the capability registry the
frontend reads to enable/disable each attachment action honestly."""

from __future__ import annotations

from attachments.capabilities import get_attachment_capabilities
from attachments.image_ops import RESIZE_PRESETS
from config.settings import Settings
from security.secrets import SecretStr


def _settings(**overrides) -> Settings:
    base = Settings(
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
        ollama_request_timeout_seconds=60,
        db_type="postgresql",
        db_host="db.example.com",
        db_port=5432,
        db_name="mydb",
        db_user="reader",
        db_password=SecretStr("secret"),
        db_connection_string=None,
        db_schema=None,
        db_odbc_driver="x",
        chroma_persist_dir="/tmp/chroma",
        chroma_collection_name="schema_ddl",
        embedding_model_name="all-MiniLM-L6-v2",
        schema_top_k=4,
        max_retries=3,
        complex_query_max_retry_bonus=2,
        max_result_rows=1000,
        query_timeout_seconds=15,
        llm_max_tokens=1024,
        insight_max_tokens=120,
        max_question_length=500,
        question_rate_limit_per_minute=10,
        llm_call_rate_limit_per_minute=20,
        cost_estimation_enabled=True,
        cost_estimation_timeout_seconds=3,
        cost_moderate_row_threshold=50_000,
        cost_high_row_threshold=1_000_000,
        log_level="INFO",
        log_redaction_level="standard",
    )
    return Settings(**{**base.__dict__, **overrides})


class TestGetAttachmentCapabilities:
    def test_vision_input_false_when_no_vision_model_configured(self):
        capabilities = get_attachment_capabilities(_settings(media_vision_model=""))
        assert capabilities.vision_input is False
        assert capabilities.vision_model is None

    def test_vision_input_true_when_vision_model_configured(self):
        capabilities = get_attachment_capabilities(_settings(media_vision_model="llava"))
        assert capabilities.vision_input is True
        assert capabilities.vision_model == "llava"

    def test_native_pdf_input_is_always_false(self):
        # No provider this app uses accepts a raw PDF as a multimodal
        # message part -- documents are always extracted to text first.
        capabilities = get_attachment_capabilities(_settings())
        assert capabilities.native_pdf_input is False

    def test_image_resize_matches_the_feature_flag(self):
        assert (
            get_attachment_capabilities(_settings(enable_chat_attachments=True)).image_resize
            is True
        )
        assert (
            get_attachment_capabilities(_settings(enable_chat_attachments=False)).image_resize
            is False
        )

    def test_resize_presets_mirror_image_ops_module(self):
        capabilities = get_attachment_capabilities(_settings())
        names = {preset.name for preset in capabilities.resize_presets}
        assert names == set(RESIZE_PRESETS.keys())
        for preset in capabilities.resize_presets:
            assert (preset.width, preset.height) == RESIZE_PRESETS[preset.name]

    def test_supported_extensions_split_images_from_documents(self):
        capabilities = get_attachment_capabilities(_settings())
        assert ".png" in capabilities.supported_image_extensions
        assert ".pdf" in capabilities.supported_document_extensions
        assert ".pdf" not in capabilities.supported_image_extensions
        assert ".png" not in capabilities.supported_document_extensions

    def test_limits_are_read_from_settings(self):
        capabilities = get_attachment_capabilities(
            _settings(max_attachment_resize_dimension_px=999, max_text_removal_regions=7)
        )
        assert capabilities.max_resize_dimension_px == 999
        assert capabilities.max_text_removal_regions == 7

    def test_ocr_and_text_removal_report_together(self):
        """Both `ocr` and `image_text_removal` reflect whether `pytesseract`
        (the Python package) is importable -- true in this environment,
        even though the Tesseract *binary* itself is separately confirmed
        absent (see tests/test_attachments_ocr_extract.py's own docstring).
        The capability flag intentionally doesn't probe the binary (that
        would cost a real subprocess call on every health-style check); a
        missing binary instead degrades a single OCR/removal request to an
        honest warning, not a flipped capability flag."""
        capabilities = get_attachment_capabilities(_settings())
        assert capabilities.ocr is True
        assert capabilities.image_text_removal is True
        assert capabilities.image_text_removal_method == "opencv_telea_inpaint"
