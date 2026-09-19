"""Blob store tests.

The write ordering is the reason this module exists, so it is tested directly
rather than inferred from behaviour: `TestDurability` records the real syscall
order and asserts the directory fsync happens *after* the rename. Getting that
wrong is invisible until a power failure, which is exactly the kind of bug a
test has to catch instead of a person.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gmail_archive.storage import BlobStore

PAYLOAD = b"the quick brown fox" * 100
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


class TestRoundTrip:
    def test_put_then_get(self, store: BlobStore) -> None:
        result = store.put(PAYLOAD)
        assert result.sha256 == DIGEST
        assert result.size_bytes == len(PAYLOAD)
        assert result.written is True
        assert store.get(DIGEST) == PAYLOAD

    def test_path_is_derived_from_the_hash(self, store: BlobStore) -> None:
        store.put(PAYLOAD)
        assert store.path_for(DIGEST) == store.root / DIGEST[:2] / DIGEST
        assert store.path_for(DIGEST).is_file()

    def test_empty_payload_is_storable(self, store: BlobStore) -> None:
        result = store.put(b"")
        assert result.size_bytes == 0
        assert store.get(result.sha256) == b""

    def test_rejects_a_malformed_digest(self, store: BlobStore) -> None:
        with pytest.raises(ValueError, match="not a sha256"):
            store.path_for("deadbeef")


class TestIdempotency:
    def test_second_put_writes_nothing(self, store: BlobStore) -> None:
        # Re-ingesting the same message must not rewrite the blob. This is a
        # property of content addressing, not of pipeline bookkeeping.
        first = store.put(PAYLOAD)
        mtime = first.path.stat().st_mtime_ns
        second = store.put(PAYLOAD)
        assert second.written is False
        assert second.path.stat().st_mtime_ns == mtime

    def test_distinct_payloads_do_not_collide(self, store: BlobStore) -> None:
        a = store.put(b"one")
        b = store.put(b"two")
        assert a.sha256 != b.sha256
        assert store.get(a.sha256) == b"one"
        assert store.get(b.sha256) == b"two"


class TestDurability:
    def test_fsync_order_is_file_then_rename_then_directory(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd: int) -> None:
            # A directory fd has no size in the usual sense; stat tells us which.
            kind = "dir" if os.fstat(fd).st_mode & 0o040000 else "file"
            events.append(f"fsync-{kind}")
            real_fsync(fd)

        def spy_replace(src: object, dst: object) -> None:
            events.append("rename")
            real_replace(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "replace", spy_replace)
        store.put(PAYLOAD)

        assert events == ["fsync-file", "rename", "fsync-dir"], events

    def test_directory_fsync_is_not_skipped(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Without the directory fsync the rename is not durable: the blob can
        # survive under its temporary name while a committed row points at the
        # final one. Guarded explicitly because it is the step most likely to be
        # "tidied away" by someone who thinks fsyncing a file is enough.
        dir_syncs = 0
        real_fsync = os.fsync

        def spy(fd: int) -> None:
            nonlocal dir_syncs
            if os.fstat(fd).st_mode & 0o040000:
                dir_syncs += 1
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", spy)
        store.put(PAYLOAD)
        assert dir_syncs == 1

    def test_no_temporary_file_survives_a_successful_write(
        self, store: BlobStore
    ) -> None:
        store.put(PAYLOAD)
        assert list(store.path_for(DIGEST).parent.glob(".tmp-*")) == []

    def test_a_failed_write_leaves_no_partial_blob(
        self, store: BlobStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A truncated file under the final name is strictly worse than an
        # orphan: a valid row would point at corrupt bytes.
        def boom(src: object, dst: object) -> None:
            raise OSError("simulated failure during rename")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="simulated failure"):
            store.put(PAYLOAD)

        assert not store.exists(DIGEST)
        shard = store.root / DIGEST[:2]
        assert list(shard.glob(".tmp-*")) == []


class TestVerify:
    def test_intact_blob_verifies(self, store: BlobStore) -> None:
        store.put(PAYLOAD)
        assert store.verify(DIGEST) is True

    def test_corrupted_blob_fails_verification(self, store: BlobStore) -> None:
        store.put(PAYLOAD)
        store.path_for(DIGEST).write_bytes(b"tampered")
        assert store.verify(DIGEST) is False

    def test_missing_blob_fails_verification_without_raising(
        self, store: BlobStore
    ) -> None:
        assert store.verify("0" * 64) is False


class TestReconciliation:
    def test_iter_blobs_lists_everything_stored(self, store: BlobStore) -> None:
        digests = {store.put(f"payload-{i}".encode()).sha256 for i in range(25)}
        assert set(store.iter_blobs()) == digests

    def test_iter_blobs_on_an_empty_store(self, store: BlobStore) -> None:
        assert store.iter_blobs() == []

    def test_temporaries_are_not_reported_as_blobs(self, store: BlobStore) -> None:
        store.put(PAYLOAD)
        leftover = store.path_for(DIGEST).parent / ".tmp-interrupted.blob"
        leftover.write_bytes(b"half a message")
        assert store.iter_blobs() == [DIGEST]

    def test_sweep_removes_temporaries_and_leaves_blobs(self, store: BlobStore) -> None:
        store.put(PAYLOAD)
        (store.path_for(DIGEST).parent / ".tmp-interrupted.blob").write_bytes(b"x")
        assert store.sweep_temporaries() == 1
        assert store.verify(DIGEST) is True
