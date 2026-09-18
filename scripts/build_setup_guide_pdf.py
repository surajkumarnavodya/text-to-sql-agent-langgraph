"""Builds docs/Local_Machine_Setup_Guide.pdf from SETUP_GUIDE.md.

Reuses `scripts/build_user_guide_pdf.py`'s markdown -> PDF pipeline (same
cover page, automatic table of contents, page-numbered footer, and small
markdown subset) rather than a second implementation -- see that script's
own module docstring for why the parser is deliberately narrow and why
duplicating it would be the wrong move. This script only supplies a
different source file and cover/footer text via `build()`'s keyword-only
overrides.

Usage:
    python scripts/build_setup_guide_pdf.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_user_guide_pdf import build  # noqa: E402

SOURCE_MD = PROJECT_ROOT / "SETUP_GUIDE.md"
OUTPUT_PDF = PROJECT_ROOT / "docs" / "Local_Machine_Setup_Guide.pdf"


def main() -> int:
    if not SOURCE_MD.exists():
        print(f"Source not found: {SOURCE_MD}", file=sys.stderr)
        return 1
    page_count = build(
        SOURCE_MD,
        OUTPUT_PDF,
        cover_subtitle=(
            "What to install, which databases to provision, and how to configure "
            "a fresh local machine to run this application end to end."
        ),
        footer_label="Text-to-SQL Dashboard — Local Machine Setup Guide",
        pdf_title="Text-to-SQL Dashboard — Local Machine Setup Guide",
        script_label="scripts/build_setup_guide_pdf.py",
    )
    print(f"Wrote {OUTPUT_PDF} ({page_count} pages) at {datetime.now(UTC).isoformat()}Z")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
