"""Processor for CSV attachments -- reads headers/rows via the standard
library `csv` module (deliberately not pandas: this codebase already has a
documented pandas/Python-3.14 segfault footgun for datetime columns, see
CLAUDE.md's "Python 3.14 gotchas" section, and a chat attachment's CSV is
exactly the kind of arbitrary, unvalidated-schema input where a stray
date/datetime-looking column could trigger it)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from attachments.models import Attachment
from attachments.processors.base import ProcessingOutcome
from attachments.processors.text_processor import decode_text_bytes
from config.settings import Settings


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    """Renders headers+rows as a compact markdown-style table -- readable by
    both a human and an LLM, and preserves column names exactly (per this
    feature's own requirement) rather than collapsing them into prose."""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        # A short row (fewer fields than headers, a real possibility for a
        # malformed CSV) is padded rather than raising -- one ragged row
        # must not fail the whole file.
        padded = row + [""] * (len(headers) - len(row))
        lines.append("| " + " | ".join(padded[: len(headers)]) + " |")
    return "\n".join(lines)


class CsvProcessor:
    source_type = "csv"

    def can_process(self, attachment: Attachment) -> bool:
        return attachment.media_type == "text/csv"

    def process(self, attachment: Attachment, settings: Settings) -> ProcessingOutcome:
        file_bytes = Path(attachment.local_path).read_bytes()  # type: ignore[arg-type]
        text, _encoding = decode_text_bytes(file_bytes)

        reader = csv.reader(io.StringIO(text))
        try:
            headers = next(reader)
        except StopIteration:
            return ProcessingOutcome(status="failed", error="This CSV file has no rows.")

        max_rows = settings.max_attachment_spreadsheet_rows
        rows: list[list[str]] = []
        total_rows = 0
        for row in reader:
            total_rows += 1
            if len(rows) < max_rows:
                rows.append(row)

        truncated = total_rows > max_rows
        table_text = _render_table(headers, rows)
        if truncated:
            table_text += f"\n\n[Truncated: showing {max_rows} of {total_rows} rows.]"

        return ProcessingOutcome(
            status="succeeded",
            extracted_text=table_text,
            extracted_tables=[{"headers": headers, "rows": rows}],
            metadata={
                "column_count": len(headers),
                "row_count": total_rows,
                "rows_included": len(rows),
                "truncated": truncated,
            },
        )
