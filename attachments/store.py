"""Process-lifetime registry of uploaded chat attachments.

Same accepted tradeoff as this project's other process-global caches
(`media_gen.cache.MediaCache`, `db.connection._cached_engine`): in-memory
only, not persisted, bounded with FIFO eviction. A restart loses in-flight
attachments -- acceptable for this app's disclosed "single-user, local-dev
oriented" scale (see `README.md`'s Limitations section), and no worse than
`media_gen`'s own generated-media cache already accepts for a comparable
per-conversation artifact.

This is also the access-control boundary for the "one user cannot access
another user's attachment" requirement: every read/delete takes the
requesting caller's `owner_subject` and returns nothing (never raises,
never distinguishes "not found" from "not yours" in its return value -- the
caller decides what error to surface) for a mismatch, the same
account-enumeration-avoidance shape `identity.exceptions
.InvalidCredentialsError` already uses elsewhere in this codebase.
"""

from __future__ import annotations

from collections import OrderedDict

from attachments.models import Attachment
from attachments.storage import delete_attachment_file

_MAX_ITEMS = 200


class AttachmentStore:
    """Bounded `attachment_id -> Attachment` registry, plus a
    `sha256 -> attachment_id` index for cross-message dedup reuse (see
    `attachments.pipeline.process_attachment`'s "don't reprocess an
    unchanged file" requirement). Construct via `get_default_attachment_store()`
    rather than directly, mirroring `media_gen.cache.get_media_cache()`."""

    def __init__(self, max_items: int = _MAX_ITEMS) -> None:
        self._max_items = max_items
        self._items: OrderedDict[str, Attachment] = OrderedDict()
        self._by_hash: dict[str, str] = {}

    def put(self, attachment: Attachment) -> None:
        """Stores or replaces `attachment` under its own `attachment_id`.
        Evicts the oldest entry first if at capacity and this is a genuinely
        new id -- FIFO, matching `MediaCache.put`'s own eviction policy."""
        is_new = attachment.attachment_id not in self._items
        if is_new and len(self._items) >= self._max_items:
            _, evicted = self._items.popitem(last=False)
            self._by_hash.pop(evicted.sha256, None)
            if evicted.local_path:
                delete_attachment_file(evicted.local_path)
        self._items[attachment.attachment_id] = attachment
        self._by_hash[attachment.sha256] = attachment.attachment_id
        self._items.move_to_end(attachment.attachment_id)

    def get(self, attachment_id: str, *, owner_subject: str | None = None) -> Attachment | None:
        """Returns the stored attachment, or `None` if it was never stored,
        already evicted/deleted, or belongs to a different owner than
        `owner_subject` (see this module's docstring)."""
        item = self._items.get(attachment_id)
        if item is None:
            return None
        if item.owner_subject is not None and item.owner_subject != owner_subject:
            return None
        return item

    def get_by_hash(self, sha256: str, *, owner_subject: str | None = None) -> Attachment | None:
        """Looks up a previously-stored attachment by content hash, scoped to
        the same ownership rule as `get` -- the entry point
        `attachments.pipeline` uses to decide "have we already processed
        this exact file for this caller" before doing any real work again."""
        attachment_id = self._by_hash.get(sha256)
        if attachment_id is None:
            return None
        return self.get(attachment_id, owner_subject=owner_subject)

    def resolve_many(
        self, attachment_ids: list[str], *, owner_subject: str | None = None
    ) -> tuple[list[Attachment], list[str]]:
        """Resolves a list of ids (e.g. `AskRequest.attachment_ids`) to their
        stored `Attachment` records.

        Returns:
            `(found, missing_ids)` -- `found` in the same relative order as
            `attachment_ids`; `missing_ids` is every id that resolved to
            nothing (never stored, evicted, or not owned by this caller),
            for the caller to report as `AttachmentErrorCode
            .ATTACHMENT_NOT_FOUND` without leaking *which* of those two
            reasons applied.
        """
        found: list[Attachment] = []
        missing: list[str] = []
        for attachment_id in attachment_ids:
            item = self.get(attachment_id, owner_subject=owner_subject)
            if item is None:
                missing.append(attachment_id)
            else:
                found.append(item)
        return found, missing

    def delete(self, attachment_id: str, *, owner_subject: str | None = None) -> bool:
        """Removes an attachment (registry entry + its on-disk bytes),
        scoped to the same ownership rule as `get`. Returns whether
        anything was actually deleted."""
        item = self.get(attachment_id, owner_subject=owner_subject)
        if item is None:
            return False
        del self._items[attachment_id]
        self._by_hash.pop(item.sha256, None)
        if item.local_path:
            delete_attachment_file(item.local_path)
        return True


_default_store: AttachmentStore | None = None


def get_default_attachment_store() -> AttachmentStore:
    """Returns the process-wide attachment store, creating it on first use --
    same lazy-singleton idiom as `media_gen.cache.get_media_cache`."""
    global _default_store
    if _default_store is None:
        _default_store = AttachmentStore()
    return _default_store
