"""Bulk Regex Renamer — business logic layer.

Renames files by regular expression, matched against each file's stem (the
name without its extension — the extension is always preserved, so a
careless global pattern cannot mangle it). A preview is always computed
first, and ``rename()`` re-derives from the exact same preview logic, so
what was shown is exactly what runs.

No rename is ever allowed to silently clobber another file: a computed
target that collides with another file's target, or with an existing file
that isn't the same file (case-only renames on a case-insensitive
filesystem are recognised and allowed), is skipped and reported rather than
executed. Renaming is not destructive the way deletion is — nothing is
removed from disk — so completed renames are undoable simply by reversing
the (old_path, new_path) pairs, with no need for ``core.recycle_store``.

Zero NiceGUI imports permitted (rule A-01).
"""
from __future__ import annotations

import asyncio
import datetime
import errno
import os
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pathspec

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.sandbox import SandboxTask, run_in_thread, run_reporting_progress
from modules.extractors.bulk_renamer.constants import (
    COUNTER_START,
    DATE_PLACEHOLDER_FORMAT,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PREVIEW,
    EVENT_PREVIEWED,
    EVENT_PROGRESS,
    PROGRESS_COMPLETE,
    PROGRESS_START,
)
from modules.extractors.bulk_renamer.models import (
    PreviewResult,
    RenamedPair,
    RenameParams,
    RenamePreviewEntry,
    RenameResult,
    UndoParams,
)
from shared.constants import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from shared.platform_info import is_windows
from shared.validators import is_safe_filename

logger = get_logger(__name__)

#: Errors that mean "this filesystem has no hard links", not "the rename
#: failed". Anything else from ``os.link`` is a real failure and propagates.
_LINK_UNSUPPORTED_ERRNOS: frozenset[int] = frozenset(
    {errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK}
)


class BulkRenamerLogic:
    """Previews and performs regex-based bulk renames."""

    def __init__(self) -> None:
        self._execution = SandboxTask()
        self._last_result: RenameResult | None = None

    async def register(self) -> None:
        """Subscribe EventBus handlers.  Call from ``on_load()``."""
        event_bus.subscribe(EVENT_PREVIEW, self._on_preview)
        event_bus.subscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.subscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("bulk_renamer.logic.registered")

    async def unregister(self) -> None:
        """Unsubscribe EventBus handlers.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_PREVIEW, self._on_preview)
        event_bus.unsubscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.unsubscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("bulk_renamer.logic.unregistered")

    # ─── EventBus handlers ────────────────────────────────────────────────────

    async def _on_preview(self, payload: Any) -> None:
        """Compute a preview and publish it.

        Args:
            payload: A RenameParams instance.
        """
        if not isinstance(payload, RenameParams):
            logger.error(
                "bulk_renamer.bad_preview_payload — type=%s", type(payload).__name__
            )
            return
        try:
            result = await run_in_thread(self.preview, payload)
            await event_bus.publish(EVENT_PREVIEWED, result)
        except Exception as exc:
            logger.error("bulk_renamer.preview_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    async def _on_cancel(self, _payload: Any) -> None:
        """Stop the in-flight operation at the user's request.

        The cancelled run reports nothing itself — this handler owns telling
        the UI, so the execute handler can stay quiet about a deliberate
        user action (RFC 0003).
        """
        if self._execution.request_cancel():
            logger.info("bulk_renamer.cancel_requested")
            await event_bus.publish(EVENT_CANCELLED, None)

    async def _on_execute(self, payload: Any) -> None:
        """Run a rename or an undo requested by the UI.

        Args:
            payload: A RenameParams or UndoParams instance.
        """
        if not isinstance(payload, RenameParams | UndoParams):
            logger.error("bulk_renamer.bad_payload — type=%s", type(payload).__name__)
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
            logger.warning("bulk_renamer.timeout — after %ds", DEFAULT_EXECUTION_TIMEOUT_SECONDS)
            await event_bus.publish(
                EVENT_ERROR,
                f"The operation exceeded {DEFAULT_EXECUTION_TIMEOUT_SECONDS}s and was stopped.",
            )
        except asyncio.CancelledError:
            # This handler task is itself the cancellation target and the cancel
            # handler has already told the UI, so a deliberate user action is
            # kept out of the error log.
            logger.info("bulk_renamer.cancelled")
        except Exception as exc:
            logger.error("bulk_renamer.execute_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    # ─── Preview ──────────────────────────────────────────────────────────────

    def preview(self, params: RenameParams) -> PreviewResult:
        """Compute what a rename run would do, without touching any file.

        Args:
            params: Validated rename parameters.

        Returns:
            One entry per candidate file, plus summary counts.
        """
        pattern = re.compile(params.pattern)
        self._validate_replacement_template(params.replacement)

        candidates = self._walk(params.source_dir, params.recursive, params.exclude_patterns)
        today = datetime.date.today().strftime(DATE_PLACEHOLDER_FORMAT)

        entries: list[RenamePreviewEntry] = []
        seen_targets: dict[Path, Path] = {}
        index = COUNTER_START

        for path in candidates:
            if pattern.search(path.stem) is None:
                entries.append(
                    RenamePreviewEntry(
                        original_path=path,
                        original_name=path.name,
                        proposed_name=path.name,
                    )
                )
                continue

            entries.append(
                self._preview_one(path, pattern, params.replacement, index, today, seen_targets)
            )
            index += 1

        matched = sum(1 for e in entries if e.matched)
        renameable = sum(1 for e in entries if e.would_rename)
        logger.info(
            "bulk_renamer.previewed — files=%d matched=%d renameable=%d",
            len(entries),
            matched,
            renameable,
        )
        return PreviewResult(
            entries=entries,
            total_files=len(entries),
            matched_count=matched,
            renameable_count=renameable,
        )

    def _preview_one(
        self,
        path: Path,
        pattern: re.Pattern[str],
        replacement: str,
        index: int,
        today: str,
        seen_targets: dict[Path, Path],
    ) -> RenamePreviewEntry:
        """Compute one file's proposed outcome.

        Args:
            path: The candidate file.
            pattern: Compiled match pattern.
            replacement: Raw replacement template (before ``{n}``/``{date}``).
            index: This match's counter value.
            today: Pre-formatted ``{date}`` value.
            seen_targets: Proposed paths already claimed earlier in this
                preview, mutated with this entry's target when it is clean.

        Returns:
            The computed preview entry.
        """
        rendered = self._render_replacement(replacement, index, today)
        try:
            new_stem = pattern.sub(rendered, path.stem)
        except re.error as exc:
            return RenamePreviewEntry(
                original_path=path,
                original_name=path.name,
                proposed_name=path.name,
                matched=True,
                unsafe=True,
                reason=f"Invalid replacement: {exc}",
            )

        new_name = f"{new_stem}{path.suffix}"

        if new_name == path.name:
            return RenamePreviewEntry(
                original_path=path,
                original_name=path.name,
                proposed_name=new_name,
                matched=True,
                reason="No change",
            )
        if not is_safe_filename(new_name):
            return RenamePreviewEntry(
                original_path=path,
                original_name=path.name,
                proposed_name=new_name,
                matched=True,
                unsafe=True,
                reason="Not a valid filename on this OS",
            )

        proposed_path = path.with_name(new_name)
        if proposed_path in seen_targets:
            return RenamePreviewEntry(
                original_path=path,
                original_name=path.name,
                proposed_name=new_name,
                matched=True,
                conflict=True,
                reason=f"Same target as {seen_targets[proposed_path].name}",
            )
        if proposed_path.exists() and not self._same_file(proposed_path, path):
            return RenamePreviewEntry(
                original_path=path,
                original_name=path.name,
                proposed_name=new_name,
                matched=True,
                conflict=True,
                reason="A different file already has this name",
            )

        seen_targets[proposed_path] = path
        return RenamePreviewEntry(
            original_path=path, original_name=path.name, proposed_name=new_name, matched=True
        )

    def _render_replacement(self, replacement: str, index: int, today: str) -> str:
        """Substitute ``{n}``/``{date}`` placeholders in a replacement template.

        Args:
            replacement: Raw template, still carrying any regex backreferences.
            index: This match's counter value.
            today: Pre-formatted ``{date}`` value.

        Returns:
            The template with placeholders resolved; backreference syntax
            (``\\1``, ``\\g<name>``) is untouched, for ``pattern.sub`` to
            resolve against the actual match.
        """
        return replacement.format(n=index, date=today)

    def _validate_replacement_template(self, replacement: str) -> None:
        """Validate a replacement template once, before it is applied per-file.

        Args:
            replacement: The raw template to validate.

        Raises:
            ValueError: When the template is not a usable format string.
        """
        try:
            replacement.format(n=COUNTER_START, date="0000-00-00")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"Invalid replacement template: {exc}") from exc

    # ─── Renaming ──────────────────────────────────────────────────────────────

    def rename(
        self,
        params: RenameParams,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> RenameResult:
        """Rename every clean, safe, actually-changed entry of the approved plan.

        The plan the user approved is executed verbatim — it is not recomputed.
        Re-deriving it here meant a file created between Preview and Run was
        renamed without ever appearing on screen, and that the ``{n}`` counter
        silently renumbered every later match. ``params.plan`` is empty only
        when no preview was supplied, in which case one is computed.

        Each entry is still re-checked against the filesystem immediately
        before it is renamed, so a plan that has gone stale is reported rather
        than applied blindly.

        Args:
            params: Validated rename parameters, normally carrying the
                approved plan.
            on_progress: Called with ``(done, total)`` as each entry is
                handled, so a large batch reports movement instead of sitting
                at 0% until it finishes (rule D-08).

        Returns:
            The outcome, including every pair renamed (for undo).

        Raises:
            ValueError: When nothing ends up renamed.
        """
        entries = params.plan or self.preview(params).entries
        matched_count = sum(1 for entry in entries if entry.matched)

        renamed: list[RenamedPair] = []
        warnings: list[str] = []
        claimed: set[Path] = set()
        total = len(entries)

        for done, entry in enumerate(entries, start=1):
            if on_progress is not None:
                on_progress(done, total)
            if not entry.would_rename:
                if entry.matched and (entry.conflict or entry.unsafe):
                    warnings.append(f"{entry.original_name}: {entry.reason}")
                continue

            new_path = entry.original_path.with_name(entry.proposed_name)
            stale = self._stale_reason(entry, new_path, claimed)
            if stale is not None:
                warnings.append(f"{entry.original_name}: {stale}")
                continue

            try:
                self._rename_without_clobbering(entry.original_path, new_path)
            except OSError as exc:
                warnings.append(f"{entry.original_name}: {exc}")
                continue
            claimed.add(new_path)
            renamed.append(RenamedPair(old_path=entry.original_path, new_path=new_path))

        if not renamed:
            if warnings:
                raise ValueError("Nothing could be renamed. " + "; ".join(warnings[:3]))
            raise ValueError(
                "Nothing to rename — no files matched, or every match already "
                "had its target name."
            )

        logger.info(
            "bulk_renamer.renamed — matched=%d renamed=%d skipped=%d",
            matched_count,
            len(renamed),
            len(warnings),
        )
        return RenameResult(
            files_renamed=len(renamed), skipped_count=len(warnings), warnings=warnings,
            renamed=renamed,
        )

    def _stale_reason(
        self, entry: RenamePreviewEntry, new_path: Path, claimed: set[Path]
    ) -> str | None:
        """Return why an approved entry can no longer be applied, or ``None``.

        The plan was computed when Preview ran; the filesystem may have moved
        on since. This is the check that turns "apply the approved plan" into
        something safe to do — the plan decides *what* to attempt, the
        filesystem still decides whether it may happen.

        Args:
            entry: The approved preview entry.
            new_path: The destination it names.
            claimed: Destinations already taken earlier in this same run.

        Returns:
            A user-facing reason to skip, or ``None`` to proceed.
        """
        if not entry.original_path.is_file():
            return "no longer exists, skipped"
        if new_path in claimed:
            return "another file in this run already took that name, skipped"
        if new_path.exists() and not self._same_file(new_path, entry.original_path):
            return "a different file now has this name, skipped"
        return None

    def _rename_without_clobbering(self, source: Path, destination: Path) -> None:
        """Rename *source* to *destination*, never replacing an existing file.

        ``Path.rename`` raises on Windows when the destination exists but on
        POSIX it replaces it silently — so on POSIX the pre-flight existence
        check in :meth:`_stale_reason` is all that stood between a race and a
        destroyed file. Creating a hard link first closes that window: the link
        fails atomically if the name is taken, and only once it succeeds is the
        old name unlinked.

        Args:
            source: The file to rename.
            destination: Its new path, in the same directory.

        Raises:
            OSError: When the rename fails, including ``FileExistsError`` when
                the destination was claimed after the pre-flight check.
        """
        if is_windows():
            source.rename(destination)
            return
        try:
            os.link(source, destination)
        except OSError as exc:
            # Filesystems that refuse hard links (some FUSE mounts, FAT) leave
            # only the checked rename — the race window is documented rather
            # than silently accepted.
            if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                raise
            logger.debug(
                "bulk_renamer.link_unsupported — falling back to rename errno=%s",
                exc.errno,
            )
            source.rename(destination)
            return
        source.unlink()

    def undo(self, params: UndoParams) -> RenameResult:
        """Reverse a previous rename run.

        Args:
            params: The pairs to reverse.

        Returns:
            The outcome of the reversal.

        Raises:
            ValueError: When nothing ends up undone.
        """
        reversed_pairs: list[RenamedPair] = []
        warnings: list[str] = []

        for pair in params.pairs:
            if not pair.new_path.is_file():
                warnings.append(f"{pair.new_path.name}: no longer exists, cannot undo")
                continue
            if pair.old_path.exists() and not self._same_file(pair.old_path, pair.new_path):
                warnings.append(
                    f"{pair.old_path.name}: a different file now has this name, cannot undo"
                )
                continue
            try:
                pair.new_path.rename(pair.old_path)
            except OSError as exc:
                warnings.append(f"{pair.new_path.name}: {exc}")
                continue
            reversed_pairs.append(RenamedPair(old_path=pair.new_path, new_path=pair.old_path))

        if not reversed_pairs:
            if warnings:
                raise ValueError("Nothing could be undone. " + "; ".join(warnings[:3]))
            raise ValueError("Nothing to undo.")

        logger.info(
            "bulk_renamer.undone — restored=%d skipped=%d",
            len(reversed_pairs),
            len(warnings),
        )
        return RenameResult(
            files_renamed=len(reversed_pairs), skipped_count=len(warnings), warnings=warnings,
            renamed=reversed_pairs,
        )

    def _same_file(self, a: Path, b: Path) -> bool:
        """Return True when *a* and *b* are the same file on disk.

        Distinguishes a genuine name collision from a case-only rename on a
        case-insensitive filesystem, where the "existing" path and the
        source path are actually the same inode.

        Args:
            a: First path.
            b: Second path.
        """
        try:
            return a.exists() and b.exists() and a.samefile(b)
        except OSError:
            return False

    # ─── BaseModule execute() shim ────────────────────────────────────────────

    async def execute(self, params: RenameParams | UndoParams) -> AsyncIterator[ProgressEvent]:
        """Run a rename or an undo, yielding progress checkpoints.

        Args:
            params: RenameParams to rename, or UndoParams to reverse a
                previous run.

        Yields:
            ProgressEvent at each checkpoint.
        """
        if isinstance(params, UndoParams):
            yield ProgressEvent(percent=PROGRESS_START, message="Undoing the last rename…")
            result = await run_in_thread(self.undo, params)
            self._last_result = result
            yield ProgressEvent(percent=PROGRESS_COMPLETE, message=self._summarise(result))
            return

        yield ProgressEvent(percent=PROGRESS_START, message="Renaming files…")
        # Renaming hundreds of files used to emit exactly two events — 0% then
        # 100% — so the bar sat still for the whole run (rule D-08). Each file
        # now reports as it goes.
        queue: list[ProgressEvent] = []

        async def relay(done: int, total: int) -> None:
            """Turn one file's completion into a progress checkpoint."""
            queue.append(
                ProgressEvent(
                    percent=self._scaled(done, total),
                    message=f"Renamed {done:,} of {total:,}…",
                )
            )

        result = await run_reporting_progress(
            lambda report: self.rename(params, report), relay
        )
        for event in queue:
            yield event

        self._last_result = result
        yield ProgressEvent(percent=PROGRESS_COMPLETE, message=self._summarise(result))

    def _scaled(self, done: int, total: int) -> int:
        """Map *done* of *total* onto the working progress band.

        Args:
            done: Entries handled so far.
            total: Entries in the run.

        Returns:
            A percentage strictly between start and completion, so the final
            100% is only ever emitted once the result exists.
        """
        if total <= 0:
            return PROGRESS_START
        span = PROGRESS_COMPLETE - PROGRESS_START - 1
        return PROGRESS_START + int(done / total * span)

    def _summarise(self, result: RenameResult) -> str:
        """Build the completion message shown in the UI.

        Args:
            result: The completed run's result.

        Returns:
            A short human-readable summary.
        """
        noun = "file" if result.files_renamed == 1 else "files"
        shortfall = "" if not result.skipped_count else f" ({result.skipped_count} skipped)"
        return f"Renamed {result.files_renamed} {noun}{shortfall}"

    # ─── Scanning ─────────────────────────────────────────────────────────────

    def _walk(self, root: Path, recursive: bool, exclude_patterns: list[str]) -> list[Path]:
        """List every file under *root* that survives the exclude patterns.

        Args:
            root: Directory to walk.
            recursive: Descend into subdirectories when True; otherwise only
                *root*'s direct children are considered.
            exclude_patterns: Gitignore-style patterns to skip.

        Returns:
            Sorted absolute paths.
        """
        spec = pathspec.PathSpec.from_lines("gitignore", exclude_patterns)
        candidates = root.rglob("*") if recursive else root.iterdir()
        matched: list[Path] = []

        for path in candidates:
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
