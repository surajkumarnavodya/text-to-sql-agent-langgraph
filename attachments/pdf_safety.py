"""Dangerous-content preflight for PDF attachments -- checked before any
text extraction/OCR, on top of (not instead of) the page-count bound and
password-protection rejection `attachments/processors/pdf_processor.py`
already has.

Neither `pypdf` nor `pymupdf` executes embedded JavaScript, launch actions,
or opens embedded files themselves (this codebase never renders a PDF in an
actual PDF-viewer runtime) -- the practical risk this guards against is a
downstream consumer (a human opening a "processed" copy, a future feature
that hands the PDF's bytes to an external tool/provider, or this app's own
`GET`-style download surfaces if one is ever added for attachments)
inheriting active content this app never flagged. Per this feature's own
"reject/quarantine active content under the default policy" requirement,
these constructs are treated as a hard rejection, not a silent pass-through.

Deliberately a **catalog-level** preflight only (the document's root
`/Root` dictionary, plus its `/Names` name tree) -- it does not walk every
page's own `/AA` (page-open/close actions) or per-annotation `/A` link
actions. That's a real, disclosed scope boundary: the catalog-level checks
below catch the three most common and well-documented "PDF malware"
vectors (embedded JavaScript, an automatic open action, embedded files)
cheaply (a handful of dict lookups, no page iteration, so this can never
itself become an expensive operation on a maliciously large page tree);
a per-page/per-annotation walk would catch more but at real added
complexity and page-count-scaling cost. See this module's own test file
for what is and isn't covered.
"""

from __future__ import annotations

from typing import Any

from pypdf import PdfReader
from pypdf.generic import IndirectObject


class PdfSafetyError(Exception):
    """Raised when a PDF fails the dangerous-content preflight. Callers
    (`attachments/processors/pdf_processor.py`) catch this and convert it
    into an ordinary `ProcessingOutcome(status="failed", error=...)`."""


def _resolve(obj: Any) -> Any:
    return obj.get_object() if isinstance(obj, IndirectObject) else obj


def check_pdf_safety(reader: PdfReader) -> None:
    """Raises `PdfSafetyError` if `reader`'s document catalog declares
    embedded JavaScript, an embedded-files name tree, an automatic open
    action, or document-level additional actions.

    Fails open (returns without raising) if the catalog itself can't be
    read -- a malformed catalog is a real problem, but it's the *existing*
    extraction step's job to reject it as a corrupt PDF, not this
    security-specific check's.
    """
    try:
        catalog = _resolve(reader.trailer["/Root"])
    except Exception:  # noqa: BLE001 - malformed catalog, not this check's concern
        return

    names = catalog.get("/Names")
    if names is not None:
        names = _resolve(names)
        if "/JavaScript" in names:
            raise PdfSafetyError(
                "This PDF contains embedded JavaScript, which is not allowed for "
                "attached documents."
            )
        if "/EmbeddedFiles" in names:
            raise PdfSafetyError(
                "This PDF contains embedded files, which is not allowed for attached " "documents."
            )

    if "/OpenAction" in catalog:
        raise PdfSafetyError(
            "This PDF declares an automatic open action, which is not allowed for "
            "attached documents."
        )

    if "/AA" in catalog:
        raise PdfSafetyError(
            "This PDF declares document-level automatic actions, which is not allowed "
            "for attached documents."
        )


__all__ = ["PdfSafetyError", "check_pdf_safety"]
