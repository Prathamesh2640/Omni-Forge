"""Duplicate Detective — business logic layer.

Two-pass detection: files are first grouped by size (cheap, no I/O beyond a
stat call), then only the files sharing a size with at least one other file
are hashed. Hashing is the expensive part, so it runs across the shared
process pool (``core.sandbox.get_process_pool``) once there are enough
candidates to justify the inter-process overhead — pure CPU work like this
gets no benefit from threads under the GIL.

Deletion always keeps exactly one file per group and routes the rest through
``core.recycle_store`` first, so a resolve run is undoable for 24 hours
(rule B-04). Each condemned file is also compared byte-for-byte against the
survivor immediately before it goes — the grouping hash is fast, not
collision-proof, and a file may have changed since the scan.

Zero NiceGUI imports permitted (rule A-01).
"""
from __future__ import annotations

import asyncio
import datetime
import filecmp
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import pathspec

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.recycle_store import recycle_paths
from core.sandbox import (
    SandboxTask,
    get_process_pool,
    run_in_thread,
    run_reporting_progress,
)
from modules.extractors.duplicate_finder.constants import (
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    EVENT_SCAN,
    EVENT_SCANNED,
    HASH_POOL_CHUNKSIZE,
    MIN_FILES_FOR_MULTIPROCESSING,
    PROGRESS_COMPLETE,
    PROGRESS_KEEPERS_CHOSEN,
    PROGRESS_START,
    SCAN_REPORT_EVERY,
)
from modules.extractors.duplicate_finder.models import (
    DuplicateFile,
    DuplicateGroup,
    KeepStrategy,
    ResolveParams,
    ResolveResult,
    ScanParams,
    ScanResult,
)
from shared.constants import DEFAULT_EXECUTION_TIMEOUT_SECONDS, MILLISECONDS_PER_SECOND
from shared.file_utils import hash_file
from shared.formatters import format_bytes

logger = get_logger(__name__)


def _hash_path(path: Path) -> str | None:
    """Hash one file's content.

    Module-level so it can be pickled for the process pool — a bound method
    or closure cannot cross the process boundary.

    Args:
        path: File to hash.

    Returns:
        The xxh3_128 hex digest, or ``None`` when the file cannot be read
        (e.g. it was deleted or became inaccessible mid-scan).
    """
    try:
        return hash_file(path)
    except OSError:
        return None


class DuplicateFinderLogic:
    """Finds and resolves duplicate files by content."""

    def __init__(self) -> None:
        self._execution = SandboxTask()
        self._last_result: ResolveResult | None = None

    async def register(self) -> None:
        """Subscribe EventBus handlers.  Call from ``on_load()``."""
        event_bus.subscribe(EVENT_SCAN, self._on_scan)
        event_bus.subscribe(EVENT_CANCEL, self._on_cancel)
        event_bus.subscribe(EVENT_EXECUTE, self._on_execute)
        logger.debug("duplicate_finder.logic.registered")

    async def unregister(self) -> None:
        """Unsubscribe EventBus handlers.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_SCAN, self._on_scan)
        event_bus.unsubscribe(EVENT_CANCEL, self._on_cancel)
        event_bus.unsubscribe(EVENT_EXECUTE, self._on_execute)
        logger.debug("duplicate_finder.logic.unregistered")

    # ─── EventBus handlers ────────────────────────────────────────────────────

    async def _on_scan(self, payload: Any) -> None:
        """Scan a directory and publish the duplicate groups found.

        Args:
            payload: A ScanParams instance.
        """
        if not isinstance(payload, ScanParams):
            logger.error(
                "duplicate_finder.bad_scan_payload — type=%s", type(payload).__name__
            )
            return
        try:
            async def report(scanned: int) -> None:
                """Relay the scan's running count to the UI."""
                await event_bus.publish(
                    EVENT_PROGRESS,
                    ProgressEvent(
                        percent=PROGRESS_START,
                        message=f"Scanned {scanned:,} files…",
                    ),
                )

            # A scan can take a long time; without this the user watched a
            # spinner with no sign of movement (rule D-08, audit §3.11f).
            result = await run_reporting_progress(
                lambda on_progress: self.scan(payload, on_progress), report
            )
            await event_bus.publish(EVENT_SCANNED, result)
        except Exception as exc:
            logger.error("duplicate_finder.scan_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    async def _on_cancel(self, _payload: Any) -> None:
        """Stop the in-flight operation at the user's request.

        The cancelled run reports nothing itself — this handler owns telling
        the UI, so the execute handler can stay quiet about a deliberate
        user action (RFC 0003).
        """
        if self._execution.request_cancel():
            logger.info("duplicate_finder.cancel_requested")
            await event_bus.publish(EVENT_CANCELLED, None)

    async def _on_execute(self, payload: Any) -> None:
        """Run a resolve requested by the UI.

        Args:
            payload: A ResolveParams instance.
        """
        if not isinstance(payload, ResolveParams):
            logger.error("duplicate_finder.bad_payload — type=%s", type(payload).__name__)
            return

        self._last_result = None
        try:
            # Rule B-02 — bounded and cancellable. Iterating the generator here
            # directly meant the sandbox's timeout applied to nothing (RFC 0003).
            await self._execution.consume(
                self.execute(payload),
                lambda event: event_bus.publish(EVENT_PROGRESS, event),
            )
            if self._last_result is not None:
                await event_bus.publish(EVENT_DONE, self._last_result)
        except TimeoutError:
            logger.warning("duplicate_finder.timeout — after %ds", DEFAULT_EXECUTION_TIMEOUT_SECONDS)
            await event_bus.publish(
                EVENT_ERROR,
                f"The operation exceeded {DEFAULT_EXECUTION_TIMEOUT_SECONDS}s and was stopped.",
            )
        except asyncio.CancelledError:
            # This handler task is itself the cancellation target and the cancel
            # handler has already told the UI, so a deliberate user action is
            # kept out of the error log.
            logger.info("duplicate_finder.cancelled")
        except Exception as exc:
            logger.error("duplicate_finder.execute_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    # ─── Scanning ─────────────────────────────────────────────────────────────

    def scan(
        self,
        params: ScanParams,
        on_progress: Callable[[int], None] | None = None,
    ) -> ScanResult:
        """Find duplicate files under a directory tree.

        Args:
            params: Scan parameters.

        Returns:
            Duplicate groups sorted by reclaimable space, largest first.
        """
        started = time.perf_counter()
        candidates = self._walk(params.source_dir, params.exclude_patterns)

        size_buckets: dict[int, list[Path]] = defaultdict(list)
        total_files = 0
        for path in candidates:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < params.min_size_bytes:
                continue
            size_buckets[size].append(path)
            total_files += 1
            if on_progress is not None and total_files % SCAN_REPORT_EVERY == 0:
                on_progress(total_files)

        # Only a size shared by at least one other file can be a duplicate —
        # this is what keeps the expensive hashing pass small.
        hash_candidates = [
            path for paths in size_buckets.values() if len(paths) >= 2 for path in paths
        ]
        digests = self._hash_all(hash_candidates)

        grouped: dict[tuple[int, str], list[DuplicateFile]] = defaultdict(list)
        for path in hash_candidates:
            digest = digests.get(str(path))
            if digest is None:
                continue
            entry = self._to_duplicate_file(path)
            if entry is not None:
                grouped[(entry.size_bytes, digest)].append(entry)

        groups = [
            DuplicateGroup(
                content_hash=digest,
                size_bytes=size,
                files=sorted(files, key=lambda f: f.modified_at, reverse=True),
            )
            for (size, digest), files in grouped.items()
            if len(files) >= 2
        ]
        groups.sort(key=lambda g: g.wasted_bytes, reverse=True)

        result = ScanResult(
            groups=groups,
            total_files_scanned=total_files,
            total_wasted_bytes=sum(g.wasted_bytes for g in groups),
            scan_duration_ms=(time.perf_counter() - started) * MILLISECONDS_PER_SECOND,
        )
        logger.info(
            "duplicate_finder.scanned — files=%d groups=%d wasted=%d duration_ms=%.0f",
            total_files,
            len(groups),
            result.total_wasted_bytes,
            result.scan_duration_ms,
        )
        return result

    def _walk(self, root: Path, exclude_patterns: list[str]) -> list[Path]:
        """List every file under *root* that survives the exclude patterns.

        Args:
            root: Directory to walk.
            exclude_patterns: Gitignore-style patterns to skip.

        Returns:
            Sorted absolute paths.
        """
        spec = pathspec.PathSpec.from_lines("gitignore", exclude_patterns)
        matched: list[Path] = []

        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                relative = path.relative_to(root)
            except (OSError, ValueError):
                continue
            if spec.match_file(str(relative).replace("\\", "/")):
                continue
            matched.append(path)

        matched.sort()
        return matched

    def _hash_all(self, paths: list[Path]) -> dict[str, str]:
        """Hash every candidate, in parallel once there are enough of them.

        Args:
            paths: Files to hash (already narrowed to same-size buckets).

        Returns:
            ``str(path) -> hex digest``, omitting files that could not be
            read.
        """
        if not paths:
            return {}
        if len(paths) < MIN_FILES_FOR_MULTIPROCESSING:
            digests: list[str | None] = [_hash_path(p) for p in paths]
        else:
            digests = self._hash_in_parallel(paths)
        return {
            str(path): digest
            for path, digest in zip(paths, digests, strict=True)
            if digest is not None
        }

    def _hash_in_parallel(self, paths: list[Path]) -> list[str | None]:
        """Hash *paths* across the shared process pool.

        Falls back to sequential hashing in this thread if the pool itself
        cannot run (e.g. process spawning is blocked in this environment) —
        a scan must degrade, never crash, when that happens.

        Args:
            paths: Files to hash.

        Returns:
            One digest (or ``None``) per path, in the same order.
        """
        try:
            pool = get_process_pool()
            return list(pool.map(_hash_path, paths, chunksize=HASH_POOL_CHUNKSIZE))
        except BrokenProcessPool as exc:
            logger.warning(
                "duplicate_finder.process_pool_broken — falling back to sequential hashing",
                exc_info=exc,
            )
            return [_hash_path(p) for p in paths]

    def _to_duplicate_file(self, path: Path) -> DuplicateFile | None:
        """Build a DuplicateFile entry, tolerating a file that vanished.

        Args:
            path: The file.

        Returns:
            The entry, or ``None`` when the file can no longer be stat'd.
        """
        try:
            stat = path.stat()
        except OSError:
            return None
        return DuplicateFile(
            path=path,
            size_bytes=stat.st_size,
            modified_at=datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.UTC),
        )

    # ─── Resolution ───────────────────────────────────────────────────────────

    async def execute(self, params: ResolveParams) -> AsyncIterator[ProgressEvent]:
        """Delete every duplicate but the chosen keeper in each group.

        Args:
            params: Validated resolve parameters.

        Yields:
            ProgressEvent at each checkpoint.

        Raises:
            ValueError: When there is nothing left to delete.
        """
        yield ProgressEvent(percent=PROGRESS_START, message="Choosing which files to keep…")

        to_delete, warnings = await run_in_thread(self._plan_deletions, params)
        if not to_delete:
            raise ValueError(
                "Nothing to delete. Every group was either empty or could not be resolved."
            )

        yield ProgressEvent(
            percent=PROGRESS_KEEPERS_CHOSEN,
            message=f"Moving {len(to_delete)} files to the recycle store…",
        )

        # Recycling is the slow part — this used to be one blocking call with
        # no report until it finished (rule D-08).
        moves: list[ProgressEvent] = []

        async def relay(done: int, total: int) -> None:
            """Turn one recycled file into a progress checkpoint."""
            span = PROGRESS_COMPLETE - PROGRESS_KEEPERS_CHOSEN - 1
            moves.append(
                ProgressEvent(
                    percent=PROGRESS_KEEPERS_CHOSEN + int(done / max(total, 1) * span),
                    message=f"Recycled {done:,} of {total:,}…",
                )
            )

        batch = await run_reporting_progress(
            lambda report: recycle_paths(to_delete, report), relay
        )
        for event in moves:
            yield event

        self._last_result = ResolveResult(
            files_deleted=batch.entry_count,
            bytes_pending_release=sum(entry.size_bytes for entry in batch.entries),
            recycle_batch_id=batch.batch_id,
            warnings=warnings,
        )
        logger.info(
            "duplicate_finder.resolved — groups=%d deleted=%d pending=%d",
            len(params.groups),
            self._last_result.files_deleted,
            self._last_result.bytes_pending_release,
        )
        yield ProgressEvent(
            percent=PROGRESS_COMPLETE, message=self._summarise(self._last_result)
        )

    def _plan_deletions(self, params: ResolveParams) -> tuple[list[Path], list[str]]:
        """Decide which files to delete and which to keep, per group.

        Args:
            params: Resolve parameters.

        Returns:
            A ``(to_delete, warnings)`` pair. A group with fewer than two
            files is skipped silently; a group where no valid file could be
            chosen to keep is skipped with a warning.
        """
        to_delete: list[Path] = []
        warnings: list[str] = []

        for group in params.groups:
            if len(group.files) < 2:
                continue
            keep = self._choose_keeper(
                group, params.strategy, params.manual_keep.get(group.content_hash)
            )
            if keep is None:
                warnings.append(
                    f"Group {group.content_hash[:12]}…: no valid file to keep, skipped"
                )
                continue
            for entry in group.files:
                if entry.path == keep:
                    continue
                if not self._is_identical(keep, entry.path):
                    warnings.append(
                        f"{entry.path.name}: content no longer matches the file being "
                        "kept, so it was left alone"
                    )
                    continue
                to_delete.append(entry.path)

        return to_delete, warnings

    def _is_identical(self, keep: Path, candidate: Path) -> bool:
        """Confirm two files really are byte-for-byte identical.

        Grouping is done on a 128-bit non-cryptographic hash, which is chosen
        for speed, not collision resistance. Deleting a file is irreversible
        from the user's point of view, so the survivor and each condemned file
        are compared directly before anything is removed — cheap insurance
        against both a hash collision and a file edited since the scan.

        Args:
            keep: The file that will survive.
            candidate: The file about to be deleted.

        Returns:
            True when the two files have identical contents.
        """
        try:
            return filecmp.cmp(keep, candidate, shallow=False)
        except OSError as exc:
            # Unreadable means unverifiable, and unverifiable means untouched.
            logger.warning(
                "duplicate_finder.compare_failed — path=%s", candidate, exc_info=exc
            )
            return False

    def _choose_keeper(
        self,
        group: DuplicateGroup,
        strategy: KeepStrategy,
        manual_path: Path | None,
    ) -> Path | None:
        """Pick the file that survives in one duplicate group.

        Args:
            group: The group being resolved.
            strategy: Which selection rule to apply.
            manual_path: The user's explicit choice, when *strategy* is
                MANUAL — must name an actual member of *group*.

        Returns:
            The path to keep, or ``None`` when MANUAL was requested without
            (or with an invalid) *manual_path*.
        """
        if strategy is KeepStrategy.MANUAL:
            if manual_path is not None and any(f.path == manual_path for f in group.files):
                return manual_path
            return None
        if strategy is KeepStrategy.OLDEST:
            return min(group.files, key=lambda f: f.modified_at).path
        return max(group.files, key=lambda f: f.modified_at).path

    def _summarise(self, result: ResolveResult) -> str:
        """Build the completion message shown in the UI.

        Args:
            result: The completed resolve run.

        Returns:
            A short human-readable summary.
        """
        noun = "file" if result.files_deleted == 1 else "files"
        return (
            f"Recycled {result.files_deleted} {noun} — "
            f"{format_bytes(result.bytes_pending_release)} recoverable in the "
            "Recycle Bin (freed when it expires)"
        )
