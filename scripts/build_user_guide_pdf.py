"""Builds docs/User_Guide.pdf from USER_GUIDE.md.

`docs/User_Guide.pdf` previously had no checked-in source at all -- three
prior revisions were each hand-authored outside this repo and simply
committed as a replacement binary (see `git log -- docs/User_Guide.pdf`),
which is exactly how it drifted out of sync with the real app (flagged as
technical debt in `docs/PRODUCTION_READINESS_REPORT.md`). This script
replaces that with a reproducible build: `USER_GUIDE.md` is the single
source of truth for content, converted into a formatted PDF (cover page,
automatic table of contents with clickable/bookmarked entries, page
numbers) rather than duplicated by hand. Re-run this after editing
USER_GUIDE.md; there is no other way the PDF should change.

The markdown handled here is deliberately a small, known subset -- exactly
what USER_GUIDE.md actually uses (headings, **bold**/*italic*/`code`
spans, `[text](url)` links, `- ` bullet lists, one `| a | b |` table, one
fenced code block) -- not a general-purpose markdown renderer. If
USER_GUIDE.md's formatting grows beyond this subset, extend `_parse_body`
rather than reaching for a markdown library; the whole point of this
script is a small, auditable, exact mapping for one specific document.

Usage:
    python scripts/build_user_guide_pdf.py

Requires `reportlab` (see requirements.txt's "Docs build" section) -- a
docs-build-time dependency only, never imported by the running app.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as canvas_module
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_MD = PROJECT_ROOT / "USER_GUIDE.md"
OUTPUT_PDF = PROJECT_ROOT / "docs" / "User_Guide.pdf"

_ACCENT = colors.HexColor("#1f4e79")
_MUTED = colors.HexColor("#5b6770")
_CODE_BG = colors.HexColor("#f2f4f5")
_RULE = colors.HexColor("#c7ccd1")


# --- Inline markdown -> reportlab paragraph markup ------------------------

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+?)`")

# The exact set of pictographic/emoji characters USER_GUIDE.md uses as UI
# icons (🔌📄🔒 etc.) -- reportlab's standard-14 fonts (Helvetica/Times/
# Courier) have no glyphs for these at all, so left alone every one renders
# as a solid tofu box in the PDF (confirmed visually while building this
# script, not assumed). U+2192 (→), U+2014 (em dash), and U+00A7 (§) are
# deliberately *not* in this set -- those three render correctly with the
# standard fonts and are used throughout the document's prose. Stripped
# rather than substituted with a text equivalent: the surrounding quoted/
# bold UI-label text already reads fine on its own (e.g. "🔌 Test
# Connection" -> "Test Connection"). Extend this set (by codepoint, with a
# comment naming the character) if USER_GUIDE.md's icon vocabulary grows --
# same "small, exact, auditable mapping" philosophy as `_parse_source`'s
# markdown subset.
_ICON_CODEPOINTS = {
    0x21AA,  # ↪ RIGHTWARDS ARROW WITH HOOK
    0x23F3,  # ⏳ HOURGLASS WITH FLOWING SAND
    0x25B6,  # ▶ BLACK RIGHT-POINTING TRIANGLE
    0x26A0,  # ⚠ WARNING SIGN
    0x2705,  # ✅ WHITE HEAVY CHECK MARK
    0x274C,  # ❌ CROSS MARK
    0xFE0F,  # VARIATION SELECTOR-16 (trailing emoji-presentation modifier)
    0x1F310,  # 🌐 GLOBE WITH MERIDIANS
    0x1F441,  # 👁 EYE
    0x1F4A1,  # 💡 ELECTRIC LIGHT BULB
    0x1F4C4,  # 📄 PAGE FACING UP
    0x1F4CA,  # 📊 BAR CHART
    0x1F4CB,  # 📋 CLIPBOARD
    0x1F4DA,  # 📚 BOOKS
    0x1F4DC,  # 📜 SCROLL
    0x1F501,  # 🔁 CLOCKWISE OPEN CIRCLE ARROWS
    0x1F504,  # 🔄 ANTICLOCKWISE OPEN CIRCLE ARROWS
    0x1F50C,  # 🔌 ELECTRIC PLUG
    0x1F50D,  # 🔍 LEFT-POINTING MAGNIFYING GLASS
    0x1F512,  # 🔒 LOCK
    0x1F517,  # 🔗 LINK SYMBOL
    0x1F5D1,  # 🗑 WASTEBASKET
    0x1F6E0,  # 🛠 HAMMER AND WRENCH
    0x1F9ED,  # 🧭 COMPASS
}
_ICON_RE = re.compile("[" + "".join(chr(cp) for cp in _ICON_CODEPOINTS) + "]+ ?")


def _inline(text: str) -> str:
    """Converts one line/paragraph of the known markdown subset to reportlab markup.

    Code spans and links are pulled out into placeholder tokens *before*
    bold/italic run, then restored afterward -- not processed last. A code
    span like `` `j***n` `` (a masked username, three literal asterisks)
    would otherwise get misread by the bold/italic regexes as real
    emphasis markup that happens to open mid-span and only closes at the
    next unrelated `**.../**` later in the same paragraph, silently
    mangling everything in between (caught by this script actually
    crashing on exactly that input during development, not by inspection).
    Placeholder tokens use no markdown-special characters, so they're inert
    to every later regex pass regardless of order.
    """
    text = _ICON_RE.sub("", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    placeholders: list[str] = []

    def _stash(markup: str) -> str:
        placeholders.append(markup)
        return f"\x00{len(placeholders) - 1}\x00"

    def _link(match: re.Match) -> str:
        label, target = match.group(1), match.group(2)
        if target.startswith(("http://", "https://")):
            return _stash(f'<link href="{target}" color="#1f4e79"><u>{label}</u></link>')
        # An internal repo-relative doc link (e.g. SECURITY.md) -- not
        # meaningful as a clickable link once this content is a standalone
        # PDF, so keep the label text only rather than a dead link.
        return label

    def _code(match: re.Match) -> str:
        # Deliberately no size="..." attribute here -- reportlab's mini-HTML
        # paragraph parser has a real bug where two or more <font size="...">
        # spans in the *same* paragraph corrupt the font size of whatever
        # plain text falls between them (confirmed with a minimal, isolated
        # reproduction outside this document during development -- not a
        # guess). Matching the surrounding text's own size and only
        # overriding face/color avoids the bug entirely, at the cost of code
        # spans no longer being visually smaller than prose.
        return _stash(f'<font face="Courier" color="#1f4e79">{match.group(1)}</font>')

    text = _LINK_RE.sub(_link, text)
    text = _CODE_RE.sub(_code, text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    for index, markup in enumerate(placeholders):
        text = text.replace(f"\x00{index}\x00", markup)
    return text


# --- Styles -----------------------------------------------------------


def _build_styles() -> dict:
    base = getSampleStyleSheet()
    styles = {
        "CoverTitle": ParagraphStyle(
            "CoverTitle", parent=base["Title"], fontSize=28, leading=34, textColor=_ACCENT
        ),
        "CoverSubtitle": ParagraphStyle(
            "CoverSubtitle",
            parent=base["Normal"],
            fontSize=13,
            leading=18,
            alignment=TA_CENTER,
            textColor=_MUTED,
            spaceBefore=6,
        ),
        "CoverMeta": ParagraphStyle(
            "CoverMeta",
            parent=base["Normal"],
            fontSize=9.5,
            leading=13,
            alignment=TA_CENTER,
            textColor=_MUTED,
        ),
        "CoverBody": ParagraphStyle(
            "CoverBody",
            parent=base["Normal"],
            fontSize=11,
            leading=16,
            alignment=TA_CENTER,
            spaceBefore=18,
        ),
        "TOCHeading": ParagraphStyle(
            "TOCHeading", parent=base["Heading1"], fontSize=18, textColor=_ACCENT
        ),
        # Named "Heading1" exactly -- afterFlowable() below matches on this
        # style name to feed the automatic TOC and PDF bookmarks.
        "Heading1": ParagraphStyle(
            "Heading1",
            parent=base["Heading1"],
            fontSize=15,
            leading=19,
            spaceBefore=4,
            spaceAfter=10,
            textColor=_ACCENT,
        ),
        "Body": ParagraphStyle(
            "Body", parent=base["Normal"], fontSize=10.3, leading=14.5, spaceAfter=8
        ),
        # A hanging indent (leftIndent positive, firstLineIndent negative by
        # the same amount) rather than reportlab's ListFlowable/ListItem --
        # the latter's bullet-glyph column left the bullet touching the
        # first word with no gap in practice; a literal bullet character
        # baked into the paragraph text is simpler and renders predictably.
        "Bullet": ParagraphStyle(
            "Bullet",
            parent=base["Normal"],
            fontSize=10.3,
            leading=14.5,
            spaceAfter=6,
            leftIndent=16,
            firstLineIndent=-16,
        ),
        "TableCell": ParagraphStyle("TableCell", parent=base["Normal"], fontSize=9, leading=12.5),
        "TableHeader": ParagraphStyle(
            "TableHeader",
            parent=base["Normal"],
            fontSize=9,
            leading=12.5,
            textColor=colors.white,
            fontName="Helvetica-Bold",
        ),
    }
    return styles


# --- Markdown body parser ----------------------------------------------


def _flush_paragraph(buf: list[str], styles: dict, story: list) -> None:
    if buf:
        story.append(Paragraph(_inline(" ".join(buf)), styles["Body"]))
        buf.clear()


def _flush_bullets(buf: list[str], styles: dict, story: list) -> None:
    if buf:
        for line in buf:
            story.append(Paragraph(f"•&nbsp;&nbsp;{_inline(line)}", styles["Bullet"]))
        story.append(Spacer(1, 2))
        buf.clear()


def _flush_table(rows: list[list[str]], styles: dict, story: list) -> None:
    if not rows:
        return
    header, *body_rows = rows
    data = [[Paragraph(_inline(cell), styles["TableHeader"]) for cell in header]]
    for row in body_rows:
        data.append([Paragraph(_inline(cell), styles["TableCell"]) for cell in row])
    table = Table(data, colWidths=[1.7 * inch, 4.6 * inch], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _ACCENT),
                ("GRID", (0, 0), (-1, -1), 0.5, _RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _CODE_BG]),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 8))
    rows.clear()


def _flush_code(buf: list[str], story: list) -> None:
    if buf:
        pre = Preformatted(
            "\n".join(buf), ParagraphStyle("Code", fontName="Courier", fontSize=9, leading=12)
        )
        boxed = Table([[pre]], colWidths=[6.3 * inch])
        boxed.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), _CODE_BG),
                    ("BOX", (0, 0), (-1, -1), 0.5, _RULE),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ]
            )
        )
        story.append(boxed)
        story.append(Spacer(1, 8))
        buf.clear()


def _parse_preface_lines(lines: list[str], styles: dict) -> list:
    """Parses plain paragraph/bullet content (no headings, tables, or code
    fences -- the preface never uses any) into flowables, reusing the same
    lazy-continuation-aware accumulation as the main body parser below."""
    story: list = []
    para_buf: list[str] = []
    bullet_buf: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            _flush_paragraph(para_buf, styles, story)
            _flush_bullets(bullet_buf, styles, story)
            continue
        if stripped.startswith("- "):
            _flush_paragraph(para_buf, styles, story)
            bullet_buf.append(stripped[2:].strip())
            continue
        if bullet_buf:
            bullet_buf[-1] = f"{bullet_buf[-1]} {stripped}"
        else:
            para_buf.append(stripped)
    _flush_paragraph(para_buf, styles, story)
    _flush_bullets(bullet_buf, styles, story)
    return story


def _parse_source(markdown_text: str, styles: dict) -> tuple[str, str, list, list]:
    """Parses USER_GUIDE.md into (title, cover_blurb, preface_story, body_story).

    `body_story` starts at the first `## ` heading -- the hand-written
    "### Contents" numbered list in between is dropped entirely, since
    this script builds its own automatic table of contents instead of
    reproducing a manually numbered one that would need to stay in sync by
    hand. Everything between the title and "### Contents" is split in two:
    just the *first* paragraph becomes `cover_blurb` (a short cover-page
    tagline -- the cover page has no room for more without overflowing),
    and everything after it becomes `preface_story` (the two-part
    explanation, "Quick start", and the optional-features note), rendered
    as ordinary flowing content on its own page right after the table of
    contents, before Part 1 begins.
    """
    lines = markdown_text.splitlines()
    title = ""

    i = 0
    # Title: the first "# " line.
    while i < len(lines):
        if lines[i].startswith("# "):
            title = lines[i][2:].strip()
            i += 1
            break
        i += 1

    # The cover blurb: just the first paragraph (up to the first blank line).
    while i < len(lines) and not lines[i].strip():
        i += 1  # skip the blank line(s) between the title and the blurb
    blurb_buf: list[str] = []
    while i < len(lines) and lines[i].strip():
        blurb_buf.append(lines[i].strip())
        i += 1
    cover_blurb = " ".join(blurb_buf)

    # Everything else up to "### Contents" is the preface.
    preface_lines: list[str] = []
    while i < len(lines) and not lines[i].startswith("### Contents"):
        preface_lines.append(lines[i])
        i += 1
    preface_story = _parse_preface_lines(preface_lines, styles)

    # Skip the numbered contents list entirely -- resume at the first "## ".
    while i < len(lines) and not lines[i].startswith("## "):
        i += 1
    body_lines = lines[i:]

    story: list = []
    para_buf: list[str] = []
    bullet_buf: list[str] = []
    table_rows: list[list[str]] = []
    code_buf: list[str] = []
    in_code = False
    first_heading = True

    def flush_all() -> None:
        _flush_paragraph(para_buf, styles, story)
        _flush_bullets(bullet_buf, styles, story)
        _flush_table(table_rows, styles, story)

    for line in body_lines:
        if in_code:
            if line.strip() == "```":
                in_code = False
                _flush_code(code_buf, story)
            else:
                code_buf.append(line)
            continue

        if line.strip().startswith("```"):
            flush_all()
            in_code = True
            continue

        if line.startswith("## "):
            flush_all()
            if not first_heading:
                story.append(PageBreak())
            first_heading = False
            heading_text = line[3:].strip()
            story.append(Paragraph(_inline(heading_text), styles["Heading1"]))
            story.append(HRFlowable(width="100%", thickness=0.75, color=_RULE, spaceAfter=10))
            continue

        stripped = line.strip()
        if not stripped:
            flush_all()
            continue

        if stripped.startswith("|"):
            _flush_paragraph(para_buf, styles, story)
            _flush_bullets(bullet_buf, styles, story)
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue  # markdown header-separator row (|---|---|) -- not data
            table_rows.append(cells)
            continue
        _flush_table(table_rows, styles, story)

        if stripped.startswith("- "):
            _flush_paragraph(para_buf, styles, story)
            bullet_buf.append(stripped[2:].strip())
            continue

        # A "lazy continuation" line -- USER_GUIDE.md wraps a bullet item's
        # or paragraph's sentence across source lines without indenting the
        # continuation (CommonMark allows this for list items too, not just
        # paragraphs), so a line that isn't blank and doesn't start a new
        # block continues whichever block is currently open, rather than
        # always starting a fresh paragraph -- otherwise a two-line bullet
        # item splits into a one-line bullet plus an orphaned, unindented
        # paragraph right after it.
        if bullet_buf:
            bullet_buf[-1] = f"{bullet_buf[-1]} {stripped}"
        else:
            para_buf.append(stripped)

    flush_all()
    return title, cover_blurb, preface_story, story


# --- Page decoration + TOC-aware document template ------------------------


class _NumberedCanvas(canvas_module.Canvas):
    """Draws "Page X of Y" -- requires buffering every page, since the
    total page count isn't known until the whole document has been laid
    out once."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_states: list = []

    def showPage(self) -> None:
        self._saved_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total_pages = len(self._saved_states)
        for state in self._saved_states:
            self.__dict__.update(state)
            if self._pageNumber > 1:  # no footer on the cover page
                self._draw_footer(total_pages)
            super().showPage()
        super().save()

    def _draw_footer(self, total_pages: int) -> None:
        self.setFont("Helvetica", 8)
        self.setFillColor(_MUTED)
        self.drawCentredString(
            LETTER[0] / 2, 0.55 * inch, f"Page {self._pageNumber} of {total_pages}"
        )
        self.drawString(0.75 * inch, 0.55 * inch, "Text-to-SQL Dashboard — User Guide")
        self.setStrokeColor(_RULE)
        self.line(0.75 * inch, 0.72 * inch, LETTER[0] - 0.75 * inch, 0.72 * inch)


class _GuideDocTemplate(BaseDocTemplate):
    """Feeds every Heading1 paragraph into the automatic TOC + PDF outline
    (bookmarks) as it's laid out -- the standard reportlab two-pass TOC
    pattern (`multiBuild` re-runs the layout once the TOC's own page count
    is known)."""

    def __init__(self, filename: str, **kwargs):
        super().__init__(filename, **kwargs)
        self._heading_count = 0

    def build(self, *args, **kwargs):
        # multiBuild() re-runs build() on this same instance several times
        # (until the TOC's own page-count effect on later page numbers
        # stabilizes) -- without resetting this counter each pass, bookmark
        # keys keep incrementing across passes instead of matching the
        # previous pass's, which makes the TOC comparison multiBuild uses
        # to detect convergence never settle.
        self._heading_count = 0
        return super().build(*args, **kwargs)

    def afterFlowable(self, flowable) -> None:
        if isinstance(flowable, Paragraph) and flowable.style.name == "Heading1":
            self._heading_count += 1
            key = f"heading-{self._heading_count}"
            self.canv.bookmarkPage(key)
            text = flowable.getPlainText()
            self.canv.addOutlineEntry(text, key, level=0, closed=False)
            self.notify("TOCEntry", (0, text, self.page, key))


def _cover_and_toc_page(canvas_obj, doc) -> None:
    canvas_obj.saveState()
    canvas_obj.setStrokeColor(_ACCENT)
    canvas_obj.setLineWidth(2)
    canvas_obj.line(
        0.75 * inch, LETTER[1] - 1.0 * inch, LETTER[0] - 0.75 * inch, LETTER[1] - 1.0 * inch
    )
    canvas_obj.restoreState()


def build(source_path: Path = SOURCE_MD, output_path: Path = OUTPUT_PDF) -> int:
    """Builds the PDF. Returns the final page count."""
    styles = _build_styles()
    markdown_text = source_path.read_text(encoding="utf-8")
    title, cover_blurb, preface_story, body_story = _parse_source(markdown_text, styles)

    story: list = []

    # --- Cover page ---
    story.append(Spacer(1, 1.6 * inch))
    story.append(Paragraph(title, styles["CoverTitle"]))
    story.append(
        Paragraph(
            "Ask a question in plain English — get validated, read-only SQL, "
            "a live result table, and an auto-picked chart.",
            styles["CoverSubtitle"],
        )
    )
    story.append(Spacer(1, 0.35 * inch))
    story.append(Paragraph(_inline(cover_blurb), styles["CoverBody"]))
    story.append(Spacer(1, 0.9 * inch))
    build_date = date.today().isoformat()
    story.append(
        Paragraph(
            f'Generated {build_date} by <font face="Courier">'
            f'scripts/build_user_guide_pdf.py</font> from <font face="Courier">'
            f"USER_GUIDE.md</font> — the single source of truth for this document. "
            f"Re-run the script after editing the markdown; do not hand-edit this PDF.",
            styles["CoverMeta"],
        )
    )
    story.append(PageBreak())

    # --- Table of contents ---
    story.append(Paragraph("Contents", styles["TOCHeading"]))
    story.append(Spacer(1, 8))
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle(
            "TOCLevel0",
            fontName="Helvetica",
            fontSize=10.5,
            leading=16,
            leftIndent=0,
        )
    ]
    story.append(toc)
    story.append(NextPageTemplate("body"))
    story.append(PageBreak())

    # --- Preface: the two-part explanation, "Quick start", and the
    # optional-features note -- everything from the markdown's intro
    # besides the one-paragraph cover_blurb above, too long to fit on the
    # cover without overflowing it now that it explains the two-part
    # structure (see _parse_source's docstring). ---
    story.extend(preface_story)
    story.append(PageBreak())

    story.extend(body_story)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = _GuideDocTemplate(
        str(output_path),
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.85 * inch,
        bottomMargin=0.9 * inch,
        title="Text-to-SQL Dashboard — User Guide",
        author="Suraj Kumar",
    )
    cover_frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="cover")
    body_frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates(
        [
            PageTemplate(id="cover", frames=[cover_frame], onPage=_cover_and_toc_page),
            PageTemplate(id="body", frames=[body_frame]),
        ]
    )
    doc.multiBuild(story, canvasmaker=_NumberedCanvas)

    # Report the actual page count back to the caller/CLI rather than
    # re-deriving it — multiBuild already knows it internally, but doesn't
    # return it, so re-read the file with pypdf for an honest, verified count.
    import pypdf

    return len(pypdf.PdfReader(str(output_path)).pages)


def main() -> int:
    if not SOURCE_MD.exists():
        print(f"Source not found: {SOURCE_MD}", file=sys.stderr)
        return 1
    page_count = build()
    print(f"Wrote {OUTPUT_PDF} ({page_count} pages) at {datetime.now(UTC).isoformat()}Z")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
