"""Unit tests for core.recycle_store (rule B-04 undo window)."""
from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from core import recycle_store
from core.recycle_store import (
    RecycleBatch,
    batch_dir,
    delete_batch,
    list_batches,
    purge_expired,
    read_batch,
    recycle_paths,
    restore_batch,
)
from shared.constants import RECYCLE_LOCAL_DIRNAME, RECYCLE_RETENTION_HOURS


@pytest.fixture(autouse=True)
def temp_recycle_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect the recycle store into a throwaway directory."""
    root = tmp_path / "recycle"
    monkeypatch.setattr(recycle_store, "recycle_root", lambda: root)
    yield root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A directory holding files that tests can recycle."""
    space = tmp_path / "workspace"
    space.mkdir()
    return space


def _make_file(directory: Path, name: str, content: bytes = b"payload") -> Path:
    """Create a file with known contents and return its path."""
    path = directory / name
    path.write_bytes(content)
    return path


def _make_tree(directory: Path, name: str) -> Path:
    """Create a small nested directory tree and return its root."""
    root = directory / name
    (root / "nested").mkdir(parents=True)
    (root / "top.txt").write_bytes(b"top")
    (root / "nested" / "deep.txt").write_bytes(b"deep")
    return root


# ─── Recycling ────────────────────────────────────────────────────────────────


class TestRecyclePaths:
    def test_source_is_removed_from_its_original_location(self, workspace: Path) -> None:
        target = _make_file(workspace, "doomed.txt")
        recycle_paths([target])
        assert not target.exists()

    def test_batch_records_the_entry(self, workspace: Path) -> None:
        target = _make_file(workspace, "doomed.txt")
        batch = recycle_paths([target])

        assert batch.entry_count == 1
        assert batch.entries[0].original_path == target.resolve()

    def test_content_is_preserved_in_the_store(self, workspace: Path) -> None:
        target = _make_file(workspace, "doomed.txt", b"important data")
        batch = recycle_paths([target])

        assert batch.entries[0].stored_path.read_bytes() == b"important data"

    def test_records_the_size_for_impact_reporting(self, workspace: Path) -> None:
        target = _make_file(workspace, "sized.bin", b"0123456789")
        batch = recycle_paths([target])
        assert batch.total_bytes == 10

    def test_recycles_a_whole_directory_tree(self, workspace: Path) -> None:
        tree = _make_tree(workspace, "build")
        batch = recycle_paths([tree])

        assert not tree.exists()
        assert batch.entries[0].is_directory is True
        assert batch.total_bytes == 7

    def test_same_basename_from_different_directories_does_not_collide(
        self, workspace: Path
    ) -> None:
        """The index prefix is what keeps these apart."""
        (workspace / "a").mkdir()
        (workspace / "b").mkdir()
        first = _make_file(workspace / "a", "index.js", b"first")
        second = _make_file(workspace / "b", "index.js", b"second")

        batch = recycle_paths([first, second])

        assert batch.entry_count == 2
        contents = {e.stored_path.read_bytes() for e in batch.entries}
        assert contents == {b"first", b"second"}

    def test_missing_paths_are_skipped_not_fatal(self, workspace: Path) -> None:
        real = _make_file(workspace, "real.txt")
        batch = recycle_paths([workspace / "ghost.txt", real])

        assert batch.entry_count == 1
        assert batch.entries[0].original_path == real.resolve()

    def test_recycling_nothing_yields_an_empty_batch(self) -> None:
        batch = recycle_paths([])
        assert batch.entry_count == 0
        assert batch.total_bytes == 0

    def test_manifest_is_written_to_disk(self, workspace: Path) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        manifest = batch_dir(batch.batch_id) / "manifest.json"

        assert json.loads(manifest.read_text(encoding="utf-8"))["batch_id"] == batch.batch_id

    def test_consecutive_batches_get_distinct_ids(self, workspace: Path) -> None:
        first = recycle_paths([_make_file(workspace, "one.txt")])
        second = recycle_paths([_make_file(workspace, "two.txt")])
        assert first.batch_id != second.batch_id


# ─── Restoring ────────────────────────────────────────────────────────────────


class TestRestoreBatch:
    def test_file_returns_to_its_original_path(self, workspace: Path) -> None:
        target = _make_file(workspace, "doomed.txt", b"important data")
        batch = recycle_paths([target])

        restored = restore_batch(batch.batch_id)

        assert restored == 1
        assert target.read_bytes() == b"important data"

    def test_directory_tree_is_restored_intact(self, workspace: Path) -> None:
        tree = _make_tree(workspace, "build")
        batch = recycle_paths([tree])

        restore_batch(batch.batch_id)

        assert (tree / "nested" / "deep.txt").read_bytes() == b"deep"

    def test_restores_every_entry_in_the_batch(self, workspace: Path) -> None:
        files = [_make_file(workspace, f"f{i}.txt") for i in range(5)]
        batch = recycle_paths(files)

        assert restore_batch(batch.batch_id) == 5
        assert all(f.exists() for f in files)

    def test_batch_directory_is_removed_after_a_full_restore(self, workspace: Path) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        restore_batch(batch.batch_id)
        assert not batch_dir(batch.batch_id).exists()

    def test_recreated_original_is_never_overwritten(self, workspace: Path) -> None:
        target = _make_file(workspace, "conflict.txt", b"recycled version")
        batch = recycle_paths([target])
        target.write_bytes(b"newer version")

        restore_batch(batch.batch_id)

        assert target.read_bytes() == b"newer version"
        assert (workspace / "conflict (1).txt").read_bytes() == b"recycled version"

    def test_restore_recreates_a_deleted_parent_directory(self, workspace: Path) -> None:
        nested_dir = workspace / "sub"
        nested_dir.mkdir()
        target = _make_file(nested_dir, "file.txt")
        batch = recycle_paths([target])
        nested_dir.rmdir()

        assert restore_batch(batch.batch_id) == 1
        assert target.exists()

    def test_unknown_batch_id_restores_nothing(self) -> None:
        assert restore_batch("20990101_000000_000000") == 0

    def test_batch_survives_when_an_entry_cannot_be_restored(
        self, workspace: Path
    ) -> None:
        """A partially failed restore must keep the remaining entries recoverable."""
        first = _make_file(workspace, "a.txt")
        second = _make_file(workspace, "b.txt")
        batch = recycle_paths([first, second])

        # Simulate one payload going missing underneath us.
        batch.entries[0].stored_path.unlink()

        assert restore_batch(batch.batch_id) == 1
        remaining = read_batch(batch.batch_id)
        assert remaining is not None
        assert remaining.entry_count == 1

    def test_restore_is_not_repeatable_after_success(self, workspace: Path) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        restore_batch(batch.batch_id)
        assert restore_batch(batch.batch_id) == 0


# ─── Expiry ───────────────────────────────────────────────────────────────────


class TestExpiry:
    def _batch_at(self, age_hours: float) -> RecycleBatch:
        """Build an in-memory batch created *age_hours* ago."""
        created = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=age_hours)
        return RecycleBatch(batch_id="test", created_at=created)

    def test_a_fresh_batch_is_not_expired(self) -> None:
        assert self._batch_at(1).is_expired() is False

    def test_a_batch_just_inside_the_window_is_not_expired(self) -> None:
        assert self._batch_at(RECYCLE_RETENTION_HOURS - 0.1).is_expired() is False

    def test_a_batch_past_the_window_is_expired(self) -> None:
        assert self._batch_at(RECYCLE_RETENTION_HOURS + 0.1).is_expired() is True

    def test_expires_at_is_the_retention_window_after_creation(self) -> None:
        batch = self._batch_at(0)
        delta = batch.expires_at() - batch.created_at
        assert delta == datetime.timedelta(hours=RECYCLE_RETENTION_HOURS)


class TestPurgeExpired:
    def test_fresh_batches_are_retained(self, workspace: Path) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])

        assert purge_expired() == 0
        assert batch_dir(batch.batch_id).exists()

    def test_expired_batches_are_deleted(self, workspace: Path) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            hours=RECYCLE_RETENTION_HOURS + 1
        )

        assert purge_expired(now=future) == 1
        assert not batch_dir(batch.batch_id).exists()

    def test_purge_only_removes_the_expired_batches(self, workspace: Path) -> None:
        old = recycle_paths([_make_file(workspace, "old.txt")])
        # Rewrite the manifest so this batch reads as two days old.
        stale = read_batch(old.batch_id)
        assert stale is not None
        stale.created_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)
        (batch_dir(old.batch_id) / "manifest.json").write_text(
            stale.model_dump_json(), encoding="utf-8"
        )
        fresh = recycle_paths([_make_file(workspace, "new.txt")])

        assert purge_expired() == 1
        assert not batch_dir(old.batch_id).exists()
        assert batch_dir(fresh.batch_id).exists()

    def test_purging_an_empty_store_is_safe(self) -> None:
        assert purge_expired() == 0

    def test_an_old_unreadable_batch_is_reclaimed(
        self, temp_recycle_root: Path
    ) -> None:
        """A corrupt manifest must not pin its payload on disk forever.

        list_batches() skips what it cannot parse, so without a separate sweep
        an unreadable batch (corrupt, or an older schema) would never expire.
        """
        stranded = temp_recycle_root / "20200101_000000_000000"
        (stranded / "files").mkdir(parents=True)
        (stranded / "files" / "orphan.bin").write_bytes(b"payload")
        (stranded / "manifest.json").write_text("{ older schema", encoding="utf-8")

        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            hours=RECYCLE_RETENTION_HOURS + 1
        )
        assert purge_expired(now=future) == 1
        assert not stranded.exists()

    def test_a_recent_unreadable_batch_is_left_alone(
        self, temp_recycle_root: Path
    ) -> None:
        """Only once it is past the retention window — it may still be in use."""
        fresh = temp_recycle_root / "20260101_000000_000000"
        fresh.mkdir(parents=True)
        (fresh / "manifest.json").write_text("{ corrupt", encoding="utf-8")

        assert purge_expired() == 0
        assert fresh.exists()


# ─── Immediate deletion (Recycle Bin "Delete now") ────────────────────────────


class TestDeleteBatch:
    def test_deletes_the_batch_and_its_payload(self, workspace: Path) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        stored = batch.entries[0].stored_path

        assert delete_batch(batch.batch_id) is True
        assert not stored.exists()
        assert not batch_dir(batch.batch_id).exists()

    def test_deleted_batch_no_longer_lists(self, workspace: Path) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        delete_batch(batch.batch_id)
        assert list_batches() == []

    def test_unknown_batch_reports_false(self) -> None:
        assert delete_batch("20990101_000000_000000") is False

    def test_deleting_does_not_restore_the_original(self, workspace: Path) -> None:
        """Delete is the opposite of restore — the file must stay gone."""
        target = _make_file(workspace, "f.txt")
        batch = recycle_paths([target])

        delete_batch(batch.batch_id)

        assert not target.exists()


# ─── Listing ──────────────────────────────────────────────────────────────────


class TestListBatches:
    def test_empty_store_lists_nothing(self) -> None:
        assert list_batches() == []

    def test_lists_every_batch(self, workspace: Path) -> None:
        recycle_paths([_make_file(workspace, "a.txt")])
        recycle_paths([_make_file(workspace, "b.txt")])
        assert len(list_batches()) == 2

    def test_newest_batch_is_listed_first(self, workspace: Path) -> None:
        first = recycle_paths([_make_file(workspace, "a.txt")])
        second = recycle_paths([_make_file(workspace, "b.txt")])
        assert [b.batch_id for b in list_batches()] == [second.batch_id, first.batch_id]

    def test_corrupt_manifest_is_skipped_not_fatal(
        self, workspace: Path, temp_recycle_root: Path
    ) -> None:
        recycle_paths([_make_file(workspace, "good.txt")])
        broken = temp_recycle_root / "20260101_000000_000000"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text("{ not json", encoding="utf-8")

        assert len(list_batches()) == 1

    def test_read_batch_returns_none_for_a_corrupt_manifest(
        self, temp_recycle_root: Path
    ) -> None:
        broken = temp_recycle_root / "bad_batch"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text("not json at all", encoding="utf-8")
        assert read_batch("bad_batch") is None


# ─── Volume awareness (RFC 0003 — never copy across volumes) ──────────────────


class TestVolumeAwareness:
    """A source on another volume must not be copied into the central store.

    ``shutil.move`` across volumes degrades to copy-then-delete, which for a
    multi-GB cache is slow, silent, and can fill the app's own drive.
    """

    def test_same_volume_uses_the_central_store(self, workspace: Path) -> None:
        source = _make_file(workspace, "f.txt")
        assert recycle_store.recycle_root_for(source) == recycle_store.recycle_root()

    def test_other_volume_gets_a_store_on_that_volume(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other_volume = tmp_path / "other_volume"
        other_volume.mkdir()
        source = _make_file(workspace, "f.txt")

        # Pretend the source sits on a different device, rooted elsewhere.
        monkeypatch.setattr(
            recycle_store, "_device_of", lambda p: 1 if p == source else 2
        )
        monkeypatch.setattr(recycle_store, "_volume_root", lambda _p: other_volume)

        root = recycle_store.recycle_root_for(source)
        assert root == other_volume / RECYCLE_LOCAL_DIRNAME

    def test_payload_is_written_to_the_source_volume(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other_volume = tmp_path / "other_volume"
        other_volume.mkdir()
        source = _make_file(workspace, "big.bin", b"payload")
        local_root = other_volume / RECYCLE_LOCAL_DIRNAME
        monkeypatch.setattr(recycle_store, "recycle_root_for", lambda _p: local_root)

        batch = recycle_paths([source])

        stored = batch.entries[0].stored_path
        assert local_root in stored.parents
        assert stored.read_bytes() == b"payload"
        # The manifest still lives centrally so one scan finds every batch.
        assert (batch_dir(batch.batch_id) / "manifest.json").is_file()
        assert len(list_batches()) == 1

    def test_restore_works_from_another_volume(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other_volume = tmp_path / "other_volume"
        other_volume.mkdir()
        source = _make_file(workspace, "f.txt", b"data")
        monkeypatch.setattr(
            recycle_store,
            "recycle_root_for",
            lambda _p: other_volume / RECYCLE_LOCAL_DIRNAME,
        )
        batch = recycle_paths([source])

        assert restore_batch(batch.batch_id) == 1
        assert source.read_bytes() == b"data"

    def test_purge_clears_the_volume_local_payload_too(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A purge that only cleaned the central store would leak the payload."""
        other_volume = tmp_path / "other_volume"
        other_volume.mkdir()
        local_root = other_volume / RECYCLE_LOCAL_DIRNAME
        monkeypatch.setattr(recycle_store, "recycle_root_for", lambda _p: local_root)
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        stored = batch.entries[0].stored_path
        assert stored.exists()

        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            hours=RECYCLE_RETENTION_HOURS + 1
        )
        assert purge_expired(now=future) == 1

        assert not stored.exists()
        assert not (local_root / batch.batch_id).exists()
        assert not batch_dir(batch.batch_id).exists()


# ─── Filesystem failures (rule B-06 — never crash the host) ───────────────────


class TestFilesystemFailures:
    def test_an_unmovable_entry_does_not_abandon_the_batch(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A locked file must not stop the remaining paths from being recycled."""
        locked = _make_file(workspace, "locked.txt")
        movable = _make_file(workspace, "movable.txt")
        real_move = recycle_store.shutil.move

        def selective_move(src: str, dst: str) -> object:
            if "locked" in src:
                raise PermissionError("file is in use by another process")
            return real_move(src, dst)

        monkeypatch.setattr(recycle_store.shutil, "move", selective_move)

        batch = recycle_paths([locked, movable])

        assert batch.entry_count == 1
        assert batch.entries[0].original_path == movable.resolve()
        assert locked.exists()

    def test_a_failed_restore_keeps_the_entry_recoverable(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = _make_file(workspace, "f.txt")
        batch = recycle_paths([target])

        def refuse_move(src: str, dst: str) -> object:
            raise PermissionError("destination is read-only")

        monkeypatch.setattr(recycle_store.shutil, "move", refuse_move)

        assert restore_batch(batch.batch_id) == 0
        surviving = read_batch(batch.batch_id)
        assert surviving is not None
        assert surviving.entry_count == 1

    def test_a_failed_purge_is_logged_not_raised(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recycle_paths([_make_file(workspace, "f.txt")])

        def refuse_rmtree(path: str) -> None:
            raise PermissionError("directory is locked")

        monkeypatch.setattr(recycle_store.shutil, "rmtree", refuse_rmtree)
        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            hours=RECYCLE_RETENTION_HOURS + 1
        )

        assert purge_expired(now=future) == 1


# ─── Manifest trust boundary (RFC 0004) ───────────────────────────────────────


class TestManifestTrustBoundary:
    """A corrupt or edited manifest must not steer rmtree/move off the store.

    Both paths in the manifest are acted on destructively, and ``purge_expired``
    runs unattended at every application startup, so the layout is verified
    rather than trusted.
    """

    def _rewrite_stored_path(self, batch_id: str, target: Path) -> None:
        """Point the batch's single entry at *target*, as a bad manifest would."""
        manifest = batch_dir(batch_id) / "manifest.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["entries"][0]["stored_path"] = str(target)
        manifest.write_text(json.dumps(payload), encoding="utf-8")

    def test_purge_refuses_to_delete_outside_the_store(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        # <victim>/files/x makes the code two-levels-up rule resolve to <victim>.
        victim = tmp_path / "documents" / "taxes"
        (victim / "files").mkdir(parents=True)
        (victim / "keep.txt").write_bytes(b"important")
        self._rewrite_stored_path(batch.batch_id, victim / "files" / "x")

        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            hours=RECYCLE_RETENTION_HOURS + 1
        )
        assert purge_expired(now=future) == 1

        assert victim.is_dir()
        assert (victim / "keep.txt").read_bytes() == b"important"
        assert not batch_dir(batch.batch_id).exists()  # the real one still goes

    def test_delete_batch_refuses_to_delete_outside_the_store(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        victim = tmp_path / "photos"
        (victim / "files").mkdir(parents=True)
        (victim / "holiday.jpg").write_bytes(b"jpeg")
        self._rewrite_stored_path(batch.batch_id, victim / "files" / "x")

        assert delete_batch(batch.batch_id) is True
        assert (victim / "holiday.jpg").is_file()

    def test_restore_refuses_an_entry_outside_the_store(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        # Restore would otherwise become an arbitrary-file mover.
        outsider = tmp_path / "elsewhere" / "files" / "secret.key"
        outsider.parent.mkdir(parents=True)
        outsider.write_bytes(b"private")
        self._rewrite_stored_path(batch.batch_id, outsider)

        assert restore_batch(batch.batch_id) == 0
        assert outsider.read_bytes() == b"private"  # untouched

    def test_restore_refuses_a_path_belonging_to_another_batch(
        self, workspace: Path
    ) -> None:
        first = recycle_paths([_make_file(workspace, "a.txt")])
        second = recycle_paths([_make_file(workspace, "b.txt")])
        # Aim the second batch's entry at the first batch's payload.
        self._rewrite_stored_path(second.batch_id, first.entries[0].stored_path)

        assert restore_batch(second.batch_id) == 0
        assert first.entries[0].stored_path.is_file()

    def test_a_well_formed_volume_local_entry_still_restores(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        # The volume-local store is a legitimate root, not just the central one.
        batch = recycle_paths([_make_file(workspace, "f.txt")])
        local = tmp_path / RECYCLE_LOCAL_DIRNAME / batch.batch_id / "files"
        local.mkdir(parents=True)
        moved = local / "000_f.txt"
        batch.entries[0].stored_path.rename(moved)
        self._rewrite_stored_path(batch.batch_id, moved)

        assert restore_batch(batch.batch_id) == 1
        assert (workspace / "f.txt").is_file()
