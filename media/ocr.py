"""On-screen text extraction from video keyframes -- via Tesseract
(`pytesseract`), the standard free/local OCR choice, independent of the
embedding-provider decision (`media/embedding.py`). Needs the system
Tesseract binary installed separately -- see `CLAUDE.md`'s Windows-specific
notes for the same "extra manual step" shape `DB_TYPE=mssql`'s system ODBC
driver requirement already has.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def extract_text(frame_path: Path) -> str:
    """Extracts on-screen text from one keyframe image.

    Fails open (returns `""`, logs a warning) on any OCR error -- a
    missing Tesseract binary, a corrupt frame, or an OCR runtime failure
    should degrade this segment's index entry to "no OCR text," never
    block ingestion, matching every other accuracy-only aid in this
    codebase's fail-open convention (`voice.stt._build_vocabulary_hint`,
    `agent.llm_client._build_golden_examples_block`).
    """
    try:
        import pytesseract
        from PIL import Image

        with Image.open(frame_path) as image:
            return pytesseract.image_to_string(image).strip()
    except Exception:
        logger.warning(
            "[media] OCR unavailable for %s, indexing without it", frame_path, exc_info=True
        )
        return ""
