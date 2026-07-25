"""Undo store for destructive operations (rule B-04).

Every module that deletes something must move it here first. Entries stay
restorable for :data:`~shared.constants.RECYCLE_RETENTION_HOURS` hours.

Layout — one directory per batch, each self-describing::

    data/recycle/20260722_030412_517293/
        manifest.json          # batch id, timestamp, original paths, sizes
        files/
            000_node_modules/
            001_report.pdf

Entries are stored under an index prefix so two originals with the same
basename never collide. The manifest is the source of truth for restores and
for expiry, so a batch survives an application restart.

Usage::

    batch = recycle_paths([Path("build"), Path("dist")])
    ...
    restore_batch(batch.batch_id)   # within the retention window
"""
from __future__ import annotations

import datetime
import os
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import BaseModel, Field

from core.logger import get_logger
from shared.constants import (
    RECYCLE_BATCH_ID_FORMAT,
    RECYCLE_DIR,
    RECYCLE_LOCAL_DIRNAME,
    RECYCLE_MANIFEST_FILENAME,
    RECYCLE_PAYLOAD_DIRNAME,
    RECYCLE_RETENTION_HOURS,
)
from shared.file_utils import directory_size, unique_destination

logger = get_logger(__name__)


class RecycledEntry(BaseModel):
    """One file or directory held in the recycle store.

    Attributes:
        original_path: Absolute path the entry was moved from.
        stored_path: Absolute path the entry now occupies inside the store.
            This lives on the *source's* volume, which is not necessarily the
            volume holding the batch manifest (see :func:`recycle_root_for`).
        size_bytes: Size at the time it was recycled.
        is_directory: True when the entry is a directory tree.
    """

    original_path: Path
    stored_path: Path
    size_bytes: int = Field(ge=0)
    is_directory: bool = False


class RecycleBatch(BaseModel):
    """A group of entries recycled by a single destructive operation.

    Attributes:
        batch_id: Timestamp-derived identifier, also the directory name.
        created_at: UTC creation time, used for expiry.
        entries: Every file or directory in the batch.
    """

    batch_id: str
    created_at: datetime.datetime
    entries: list[RecycledEntry] = Field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        """Combined size of every entry in the batch."""
        return sum(entry.size_bytes for entry in self.entries)

    @property
    def entry_count(self) -> int:
        """Number of entries in the batch."""
        return len(self.entries)

    def expires_at(self) -> datetime.datetime:
        """Return the moment this batch becomes eligible for purging."""
        return self.created_at + datetime.timedelta(hours=RECYCLE_RETENTION_HOURS)

    def is_expired(self, now: datetime.datetime | None = None) -> bool:
        """Return True when the retention window has elapsed.

        Args:
            now: Reference time; defaults to the current UTC time.
        """
        return (now or _utc_now()) >= self.expires_at()


def _utc_now() -> datetime.datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.datetime.now(datetime.UTC)


def recycle_root() -> Path:
    """Return the root directory holding every recycle batch."""
    return Path(RECYCLE_DIR)


def batch_dir(batch_id: str) -> Path:
    """Return the directory for *batch_id*.

    Args:
        batch_id: The batch identifier.
    """
    return recycle_root() / batch_id


def _manifest_path(batch_id: str) -> Path:
    """Return the manifest path for *batch_id*."""
    return batch_dir(batch_id) / RECYCLE_MANIFEST_FILENAME


def _payload_dir_in(root: Path, batch_id: str) -> Path:
    """Return the payload directory for *batch_id* beneath *root*.

    Args:
        root: A store root, from :func:`recycle_root_for`.
        batch_id: The batch identifier.
    """
    return root / batch_id / RECYCLE_PAYLOAD_DIRNAME


def _device_of(path: Path) -> int:
    """Return the device id *path* lives on.

    Falls back to the nearest existing ancestor, so a destination that has not
    been created yet still resolves to the right volume.

    Args:
        path: The path to probe.

    Returns:
        The ``st_dev`` of *path* or its closest existing parent.
    """
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe.stat().st_dev


def _volume_root(path: Path) -> Path:
    """Return the mount point (volume root) that *path* belongs to.

    On Windows this is the drive root; on POSIX it is the nearest enclosing
    mount point, so a source under ``/mnt/data`` stays on ``/mnt/data``.

    Args:
        path: The path to locate.

    Returns:
        The volume root directory.
    """
    current = path.resolve()
    if not current.is_dir():
        current = current.parent
    while current != current.parent:
        if os.path.ismount(current):
            return current
        current = current.parent
    return current


def recycle_root_for(source: Path) -> Path:
    """Return the store root to hold *source*, on the source's own volume.

    Moving a file to another volume is not a rename — it is a full copy plus a
    delete. For a multi-gigabyte cache that is slow, invisible to the progress
    bar, and can fill the application's own drive. So anything already sharing
    the app's volume goes to the central store, and anything else gets a store
    on its own volume (see RFC 0003).

    Args:
        source: The path about to be recycled.

    Returns:
        The central store root, or a volume-local one.
    """
    central = recycle_root()
    try:
        central.mkdir(parents=True, exist_ok=True)
        if _device_of(source) == _device_of(central):
            return central
    except OSError as exc:
        # If the volume cannot be probed, the central store is the safe default.
        logger.debug("recycle.volume_probe_failed — path=%s reason=%s", source, exc)
        return central
    return _volume_root(source) / RECYCLE_LOCAL_DIRNAME


def _is_valid_store_path(stored_path: Path, batch_id: str) -> bool:
    """Return True when *stored_path* really lives inside this batch's payload.

    The manifest is ordinary JSON in a predictable location, and both of its
    paths are acted on destructively — ``_remove_batch_dir`` rmtree's the
    directory two levels above ``stored_path``, and ``restore_batch`` moves the
    file out of it. A corrupt or hand-edited manifest would otherwise aim those
    operations anywhere on disk, and the startup purge runs unattended. So the
    layout this module writes is verified before anything is acted upon
    (see ``docs/rfcs/0004``).

    A valid entry is ``<root>/<batch_id>/files/<name>``, where ``<root>`` is the
    central store or a volume-local ``.omniforge-recycle``. Requiring the
    ``<batch_id>`` component to match also stops one batch's manifest from
    reaching into another's payload.

    Args:
        stored_path: The path recorded in the manifest.
        batch_id: The batch the entry claims to belong to.

    Returns:
        True when the path is inside a real store directory for *batch_id*.
    """
    payload = stored_path.parent
    batch_directory = payload.parent
    root = batch_directory.parent
    return (
        payload.name == RECYCLE_PAYLOAD_DIRNAME
        and batch_directory.name == batch_id
        and (root.name == RECYCLE_LOCAL_DIRNAME or root == recycle_root())
    )


def _write_manifest(batch: RecycleBatch) -> None:
    """Persist *batch* to its manifest file."""
    _manifest_path(batch.batch_id).write_text(
        batch.model_dump_json(indent=2), encoding="utf-8"
    )


def read_batch(batch_id: str) -> RecycleBatch | None:
    """Load a batch by ID.

    Args:
        batch_id: The batch identifier.

    Returns:
        The batch, or ``None`` when it is absent or its manifest is unreadable.
    """
    path = _manifest_path(batch_id)
    try:
        return RecycleBatch.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("recycle.manifest_unreadable — batch=%s reason=%s", batch_id, exc)
        return None


def recycle_paths(
    paths: Iterable[Path],
    on_progress: Callable[[int, int], None] | None = None,
) -> RecycleBatch:
    """Move *paths* into a new recycle batch instead of deleting them.

    Paths that do not exist are skipped. Each entry is measured before the
    move so the batch can report how much space is recoverable.

    Args:
        paths: Files and/or directories to recycle.
        on_progress: Called with ``(done, total)`` after each entry is handled.
            This is the longest-running operation in the application — a purge
            can move thousands of files — and without it the callers sat on a
            single progress event for the entire move (rule D-08). ``total`` is
            0 until *paths* has been materialised, so callers should treat a
            zero total as "count not yet known".

    Returns:
        The created batch. It has no entries when nothing existed to move.

    Raises:
        OSError: When the batch directory cannot be created.
    """
    created_at = _utc_now()
    batch_id = created_at.strftime(RECYCLE_BATCH_ID_FORMAT)
    # The manifest always lives in the central store, so one scan still finds
    # every batch even when the payloads are spread across volumes.
    batch_dir(batch_id).mkdir(parents=True, exist_ok=True)

    batch = RecycleBatch(batch_id=batch_id, created_at=created_at)
    # Materialised so the progress callback can report a real denominator.
    sources = list(paths)
    total = len(sources)

    for index, source in enumerate(sources):
        if on_progress is not None:
            on_progress(index + 1, total)
        if not source.exists():
            logger.debug("recycle.skip_missing — path=%s", source)
            continue

        is_directory = source.is_dir()
        size_bytes, _ = directory_size(source)
        original_path = source.resolve()
        # Resolved before the move, while the source still exists to be stat'd.
        payload = _payload_dir_in(recycle_root_for(source), batch_id)
        stored_path = payload / f"{index:03d}_{source.name}"

        try:
            payload.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(stored_path))
        except OSError as exc:
            # One unmovable entry must not abandon the rest of the batch.
            logger.error("recycle.move_failed — path=%s", source, exc_info=exc)
            continue

        batch.entries.append(
            RecycledEntry(
                original_path=original_path,
                stored_path=stored_path,
                size_bytes=size_bytes,
                is_directory=is_directory,
            )
        )

    _write_manifest(batch)
    logger.info(
        "recycle.batch_created — batch=%s entries=%d bytes=%d",
        batch_id,
        batch.entry_count,
        batch.total_bytes,
    )
    return batch


def restore_batch(batch_id: str) -> int:
    """Move every entry in *batch_id* back to its original location.

    An original path that has been re-created in the meantime is never
    overwritten — the restored copy gets a ``(n)`` suffix instead. The batch
    directory is removed once every entry has been restored.

    Args:
        batch_id: The batch to restore.

    Returns:
        The number of entries successfully restored.
    """
    batch = read_batch(batch_id)
    if batch is None:
        return 0

    restored = 0
    failed: list[RecycledEntry] = []

    for entry in batch.entries:
        stored = entry.stored_path
        if not _is_valid_store_path(stored, batch_id):
            # The entry does not name a file this store owns, so moving it
            # would be acting on the manifest's word alone (RFC 0004).
            logger.error(
                "recycle.entry_outside_store — batch=%s path=%s", batch_id, stored
            )
            failed.append(entry)
            continue
        if not stored.exists():
            logger.warning("recycle.entry_missing — batch=%s name=%s", batch_id, stored.name)
            failed.append(entry)
            continue
        try:
            entry.original_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stored), str(unique_destination(entry.original_path)))
            restored += 1
        except OSError as exc:
            logger.error(
                "recycle.restore_failed — batch=%s name=%s",
                batch_id,
                stored.name,
                exc_info=exc,
            )
            failed.append(entry)

    if failed:
        # Keep the batch alive so the user can retry the entries that failed.
        batch.entries = failed
        _write_manifest(batch)
    else:
        _remove_batch_dir(batch_id)

    logger.info("recycle.restored — batch=%s count=%d", batch_id, restored)
    return restored


def list_batches() -> list[RecycleBatch]:
    """Return every readable batch, newest first.

    Returns:
        Batches sorted by creation time descending. Unreadable batches are
        skipped rather than raising.
    """
    root = recycle_root()
    if not root.is_dir():
        return []

    batches = [
        batch
        for entry in root.iterdir()
        if entry.is_dir() and (batch := read_batch(entry.name)) is not None
    ]
    batches.sort(key=lambda b: b.created_at, reverse=True)
    return batches


def delete_batch(batch_id: str) -> bool:
    """Delete a batch and its payloads immediately, before expiry.

    The counterpart to :func:`restore_batch` for the Recycle Bin UI: the user
    has decided they do not want these files back, so the space is released now
    rather than at the end of the retention window.

    Args:
        batch_id: The batch to delete.

    Returns:
        True when the batch existed and was removed.
    """
    if not batch_dir(batch_id).is_dir():
        return False
    _remove_batch_dir(batch_id)
    logger.info("recycle.deleted — batch=%s", batch_id)
    return True


def purge_expired(now: datetime.datetime | None = None) -> int:
    """Delete every batch whose retention window has elapsed.

    Args:
        now: Reference time; defaults to the current UTC time.

    Returns:
        The number of batches purged.
    """
    reference = now or _utc_now()
    purged = 0
    for batch in list_batches():
        if batch.is_expired(reference):
            _remove_batch_dir(batch.batch_id, batch)
            purged += 1
    purged += _purge_unreadable(reference)
    if purged:
        logger.info("recycle.purged — batches=%d", purged)
    return purged


def _purge_unreadable(reference: datetime.datetime) -> int:
    """Remove long-dead batch directories whose manifest cannot be parsed.

    :func:`list_batches` skips an unreadable manifest, so without this a corrupt
    batch — or one written by an older schema — would pin its payload on disk
    forever. Age is taken from the directory's own mtime, since the manifest
    that would normally carry the timestamp is precisely what cannot be read.

    Args:
        reference: The current time to measure age against.

    Returns:
        The number of directories removed.
    """
    root = recycle_root()
    if not root.is_dir():
        return 0

    cutoff = reference - datetime.timedelta(hours=RECYCLE_RETENTION_HOURS)
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir() or read_batch(entry.name) is not None:
            continue
        try:
            modified = datetime.datetime.fromtimestamp(
                entry.stat().st_mtime, tz=datetime.UTC
            )
        except OSError as exc:
            logger.debug("recycle.age_probe_failed — batch=%s reason=%s", entry.name, exc)
            continue
        if modified <= cutoff:
            # The manifest is what could not be parsed, so there are no entry
            # paths to chase — removing the directory itself is the whole job.
            logger.info("recycle.purging_unreadable — batch=%s", entry.name)
            _remove_paths({entry}, entry.name)
            removed += 1
    return removed


def _remove_batch_dir(batch_id: str, batch: RecycleBatch | None = None) -> None:
    """Delete a batch's manifest directory and every volume-local payload.

    A batch's payloads may live on several volumes (see :func:`recycle_root_for`),
    so each one is removed as well as the central manifest directory. Read the
    manifest first — deleting the central directory destroys it.

    Args:
        batch_id: The batch to remove.
        batch: An already-parsed manifest, when the caller has one. Passing it
            avoids a redundant re-read (and a duplicate warning when the
            manifest is unreadable).
    """
    # Derived from batch_id, not read from the manifest — always safe.
    targets: set[Path] = {batch_dir(batch_id)}
    resolved = batch if batch is not None else read_batch(batch_id)
    if resolved is not None:
        for entry in resolved.entries:
            # stored_path is <root>/<batch_id>/files/<name>; two levels up is
            # the batch's own directory on that volume. Verified first — this
            # set is handed to shutil.rmtree, and the startup purge runs it
            # unattended (RFC 0004).
            if not _is_valid_store_path(entry.stored_path, batch_id):
                logger.error(
                    "recycle.refusing_purge_outside_store — batch=%s path=%s",
                    batch_id,
                    entry.stored_path,
                )
                continue
            targets.add(entry.stored_path.parent.parent)
    _remove_paths(targets, batch_id)


def _remove_paths(targets: set[Path], batch_id: str) -> None:
    """Delete each directory in *targets*, logging rather than raising.

    Args:
        targets: Directories to remove.
        batch_id: The owning batch, for log context only.
    """
    for target in targets:
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.error(
                "recycle.purge_failed — batch=%s path=%s", batch_id, target, exc_info=exc
            )
