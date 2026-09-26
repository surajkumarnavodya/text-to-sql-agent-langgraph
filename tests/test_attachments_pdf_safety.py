"""Unit tests for attachments/pdf_safety.py -- the dangerous-content
preflight for PDF attachments.

The JavaScript and embedded-file fixtures are real PDFs built with `pypdf`'s
own writer helpers (`add_js`/`add_attachment`) -- genuinely exercising the
same `/Names` catalog structure a real malicious PDF would use, without
checking any real malware sample into the repository. `/OpenAction`/`/AA`
are exercised against a minimal fake catalog object (a plain dict, which
satisfies the same `.get()`/`in` duck-typed interface `check_pdf_safety`
actually uses) since `pypdf`'s writer has no built-in helper for those two
specifically -- this still tests the real function's real logic, just
against a hand-built input shape for the cases without a convenient
real-PDF-writer helper.
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfReader, PdfWriter

from attachments.pdf_safety import PdfSafetyError, check_pdf_safety


def _reader_with_js() -> PdfReader:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_js('app.alert("hello");')
    buffer = io.BytesIO()
    writer.write(buffer)
    return PdfReader(io.BytesIO(buffer.getvalue()))


def _reader_with_embedded_file() -> PdfReader:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_attachment("note.txt", b"hello world")
    buffer = io.BytesIO()
    writer.write(buffer)
    return PdfReader(io.BytesIO(buffer.getvalue()))


def _plain_reader() -> PdfReader:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return PdfReader(io.BytesIO(buffer.getvalue()))


class _FakeReader:
    """A minimal stand-in exposing only `.trailer["/Root"]` -- for the
    /OpenAction and /AA cases pypdf's writer has no direct helper for."""

    def __init__(self, root: dict):
        self.trailer = {"/Root": root}


class TestCheckPdfSafety:
    def test_an_ordinary_pdf_passes(self):
        check_pdf_safety(_plain_reader())  # no raise

    def test_rejects_a_pdf_with_embedded_javascript(self):
        with pytest.raises(PdfSafetyError, match="JavaScript"):
            check_pdf_safety(_reader_with_js())

    def test_rejects_a_pdf_with_an_embedded_file(self):
        with pytest.raises(PdfSafetyError, match="embedded files"):
            check_pdf_safety(_reader_with_embedded_file())

    def test_rejects_a_pdf_with_an_open_action(self):
        check_pdf_safety(_FakeReader({}))  # sanity: empty catalog is safe
        with pytest.raises(PdfSafetyError, match="automatic open action"):
            check_pdf_safety(_FakeReader({"/OpenAction": {"/S": "/Launch"}}))

    def test_rejects_a_pdf_with_document_level_additional_actions(self):
        with pytest.raises(PdfSafetyError, match="document-level automatic actions"):
            check_pdf_safety(_FakeReader({"/AA": {"/WC": "..."}}))

    def test_fails_open_when_the_catalog_cannot_be_read_at_all(self):
        """A malformed catalog is the *extraction* step's problem to reject
        as a corrupt PDF -- this security-specific check must not itself
        raise (or crash) on a document it can't even inspect."""

        class _BrokenReader:
            trailer: dict = {}  # no "/Root" key at all -> KeyError internally

        check_pdf_safety(_BrokenReader())  # no raise
