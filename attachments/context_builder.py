"""Assembles every successfully-processed attachment's extracted content
into one delimited context block, ready to inject into a generation prompt.

Deliberately excludes anything that failed processing -- never claims an
attachment contains information it couldn't actually extract (the failure
itself is surfaced to the user directly via the API's per-attachment error,
a separate, more appropriate layer than baking it into the model prompt).
"""

from __future__ import annotations

import hashlib

from attachments.models import ProcessedAttachment

_HEADER = (
    "The following files were attached by the user. Use them as evidence "
    "when answering, and say explicitly when a piece of information comes "
    "from one of these attachments rather than from your own knowledge."
)
_FOOTER = "[END ATTACHMENTS]"

# A per-attachment share of the total budget must never be so small that
# truncation destroys an attachment's usefulness entirely -- this is the
# floor even when many attachments are present (bounded anyway by
# Settings.max_attachments_per_message, so the floor can never be starved
# indefinitely by an unbounded attachment count).
_MIN_PER_ATTACHMENT_CHARS = 500


def build_attachment_context(
    processed: list[ProcessedAttachment], max_chars: int
) -> tuple[str, bool]:
    """Builds the delimited attachment-context block for a generation prompt.

    Args:
        processed: Every attachment considered for this question (mixed
            statuses -- only "succeeded" ones with non-empty extracted text
            actually contribute; image-only attachments with no
            `extracted_text` are also skipped here, since their content
            reaches the model as an image content block instead, built
            separately by `attachments.graph`'s `build_multimodal_message`
            node).
        max_chars: The hard ceiling on combined attachment context length
            (`Settings.max_attachment_text_chars`) -- split evenly across
            every contributing attachment (never letting one huge file
            consume the whole budget), with an explicit truncation notice
            when any attachment's content had to be cut.

    Returns:
        `(context_text, was_truncated)` -- `context_text` is `""` (not the
        header/footer alone) when nothing qualifies, so a caller can
        cheaply check `if context_text:` rather than parsing it.
    """
    candidates = [
        item
        for item in processed
        if item.get("processing_status") == "succeeded" and item.get("extracted_text")
    ]
    if not candidates:
        return "", False

    # De-dupe identical extracted content -- e.g. the same document
    # attached under two different attachment_ids across a conversation's
    # history. Keeps the first occurrence's ordering.
    seen_hashes: set[str] = set()
    deduped: list[ProcessedAttachment] = []
    for item in candidates:
        text = item["extracted_text"]
        digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        deduped.append(item)

    per_attachment_budget = max(max_chars // len(deduped), _MIN_PER_ATTACHMENT_CHARS)

    sections: list[str] = []
    truncated_any = False
    used_chars = 0
    for index, item in enumerate(deduped, start=1):
        remaining = max_chars - used_chars
        if remaining <= 0:
            truncated_any = True
            break
        budget = min(per_attachment_budget, remaining)
        text = item["extracted_text"]
        if len(text) > budget:
            text = text[:budget]
            truncated_any = True
        used_chars += len(text)

        type_label = (item.get("source_type") or item.get("media_type") or "unknown").upper()
        sections.append(
            f"[ATTACHMENT {index}]\n"
            f"Filename: {item.get('filename', 'unknown')}\n"
            f"Type: {type_label}\n"
            f"Content:\n{text}"
        )

    parts = [_HEADER, *sections]
    if truncated_any:
        parts.append("[Note: some attachment content was truncated to fit the available context.]")
    parts.append(_FOOTER)
    return "\n\n".join(parts), truncated_any
