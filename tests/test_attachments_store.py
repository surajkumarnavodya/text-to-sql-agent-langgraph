"""Unit tests for attachments/store.py."""

from __future__ import annotations

from attachments.models import Attachment
from attachments.store import AttachmentStore


def _make_attachment(**overrides) -> Attachment:
    defaults = dict(
        original_filename="a.txt",
        safe_filename="a.txt",
        media_type="text/plain",
        extension=".txt",
        size_bytes=10,
        sha256="hash-a",
    )
    return Attachment(**{**defaults, **overrides})


class TestAttachmentStore:
    def test_put_and_get_round_trip(self):
        store = AttachmentStore()
        att = _make_attachment()
        store.put(att)
        assert store.get(att.attachment_id) is att

    def test_get_missing_id_returns_none(self):
        store = AttachmentStore()
        assert store.get("does-not-exist") is None

    def test_ownership_check_blocks_a_different_caller(self):
        store = AttachmentStore()
        att = _make_attachment(owner_subject="alice")
        store.put(att)

        assert store.get(att.attachment_id, owner_subject="alice") is not None
        assert store.get(att.attachment_id, owner_subject="bob") is None
        assert store.get(att.attachment_id, owner_subject=None) is None

    def test_no_owner_means_accessible_to_anyone(self):
        """An attachment stored with owner_subject=None (unauthenticated/
        'none'/'static_token' auth modes) has no real per-caller identity to
        scope against -- see Attachment.owner_subject's own docstring."""
        store = AttachmentStore()
        att = _make_attachment(owner_subject=None)
        store.put(att)
        assert store.get(att.attachment_id, owner_subject="anyone") is not None

    def test_get_by_hash(self):
        store = AttachmentStore()
        att = _make_attachment(sha256="unique-hash")
        store.put(att)
        assert store.get_by_hash("unique-hash") is att
        assert store.get_by_hash("other-hash") is None

    def test_resolve_many_splits_found_and_missing(self):
        store = AttachmentStore()
        att1 = _make_attachment(sha256="h1")
        att2 = _make_attachment(sha256="h2")
        store.put(att1)
        store.put(att2)

        found, missing = store.resolve_many([att1.attachment_id, "ghost-id", att2.attachment_id])
        assert [a.attachment_id for a in found] == [att1.attachment_id, att2.attachment_id]
        assert missing == ["ghost-id"]

    def test_resolve_many_respects_ownership(self):
        store = AttachmentStore()
        att = _make_attachment(owner_subject="alice")
        store.put(att)
        found, missing = store.resolve_many([att.attachment_id], owner_subject="bob")
        assert found == []
        assert missing == [att.attachment_id]

    def test_delete_removes_entry_and_returns_true(self):
        store = AttachmentStore()
        att = _make_attachment()
        store.put(att)
        assert store.delete(att.attachment_id) is True
        assert store.get(att.attachment_id) is None

    def test_delete_missing_returns_false(self):
        store = AttachmentStore()
        assert store.delete("ghost-id") is False

    def test_delete_respects_ownership(self):
        store = AttachmentStore()
        att = _make_attachment(owner_subject="alice")
        store.put(att)
        assert store.delete(att.attachment_id, owner_subject="bob") is False
        assert store.get(att.attachment_id, owner_subject="alice") is not None

    def test_fifo_eviction_when_at_capacity(self):
        store = AttachmentStore(max_items=2)
        att1 = _make_attachment(sha256="h1")
        att2 = _make_attachment(sha256="h2")
        att3 = _make_attachment(sha256="h3")
        store.put(att1)
        store.put(att2)
        store.put(att3)  # should evict att1 (oldest)

        assert store.get(att1.attachment_id) is None
        assert store.get(att2.attachment_id) is not None
        assert store.get(att3.attachment_id) is not None

    def test_replacing_an_existing_id_does_not_evict(self):
        store = AttachmentStore(max_items=2)
        att = _make_attachment(sha256="h1")
        store.put(att)
        updated = att.with_processing_result(status="succeeded", extracted_text="done")
        store.put(updated)

        assert len(store._items) == 1  # still one entry, not two
        assert store.get(att.attachment_id).extracted_text == "done"
