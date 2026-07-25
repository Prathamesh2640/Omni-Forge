"""Smart File Filter — business logic layer.

Scans a directory tree, reports what extensions live in it, then extracts the
chosen ones by copy, move, zip or manifest.

Move relocates each file rather than copying it — a rename on the same volume,
so it needs no extra space. It is reversible because the ``(source,
destination)`` pairs are remembered, the same way ``bulk_renamer`` undoes a
rename; nothing is removed from disk, so the recycle store is not involved.

Zero NiceGUI imports permitted (rule A-01).
"""
from __future__ import annotations

import asyncio
import datetime
import zipfile
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pathspec

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.sandbox import SandboxTask, run_in_thread, run_reporting_progress
from modules.extractors.file_filter.constants import (
    ARCHIVE_NAME_TEMPLATE,
    COPY_DIR_TEMPLATE,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    EVENT_SCAN,
    EVENT_SCANNED,
    EVENT_UNDO,
    MANIFEST_COLUMNS,
    MANIFEST_HEADER,
    MANIFEST_NAME_TEMPLATE,
    MANIFEST_ROW_TEMPLATE,
    NO_EXTENSION_LABEL,
    OUTPUT_SUBDIR,
    PROGRESS_COMPLETE,
    PROGRESS_SCAN_DONE,
    PROGRESS_START,
    PROGRESS_WORK_END,
    SCAN_REPORT_EVERY,
    TIMESTAMP_FORMAT,
)
from modules.extractors.file_filter.models import (
    ExtensionCount,
    FilterParams,
    FilterResult,
    MovedPair,
    OutputMode,
    ScanParams,
    ScanResult,
    UndoParams,
)
from shared.constants import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from shared.file_utils import safe_copy, safe_move
from shared.validators import validate_write_target

logger = get_logger(__name__)


class FileFilterLogic:
    """Scans directories and extracts files by extension."""

    def __init__(self) -> None:
        self._execution = SandboxTask()
        self._last_result: FilterResult | None = None

    async def register(self) -> None:
        """Subscribe EventBus handlers.  Call from ``on_load()``."""
        event_bus.subscribe(EVENT_SCAN, self._on_scan)
        event_bus.subscribe(EVENT_CANCEL, self._on_cancel)
        event_bus.subscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.subscribe(EVENT_UNDO, self._on_undo)
        logger.debug("file_filter.logic.registered")

    async def unregister(self) -> None:
        """Unsubscribe EventBus handlers.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_SCAN, self._on_scan)
        event_bus.unsubscribe(EVENT_CANCEL, self._on_cancel)
        event_bus.unsubscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.unsubscribe(EVENT_UNDO, self._on_undo)
        logger.debug("file_filter.logic.unregistered")

    # ─── EventBus handlers ────────────────────────────────────────────────────

    async def _on_scan(self, payload: Any) -> None:
        """Scan a directory and publish the extension breakdown.

        Args:
            payload: A ScanParams instance.
        """
        if not isinstance(payload, ScanParams):
            logger.error("file_filter.bad_scan_payload — type=%s", type(payload).__name__)
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
            logger.error("file_filter.scan_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    async def _on_cancel(self, _payload: Any) -> None:
        """Stop the in-flight operation at the user's request.

        The cancelled run reports nothing itself — this handler owns telling
        the UI, so the execute handler can stay quiet about a deliberate
        user action (RFC 0003).
        """
        if self._execution.request_cancel():
            logger.info("file_filter.cancel_requested")
            await event_bus.publish(EVENT_CANCELLED, None)

    async def _on_execute(self, payload: Any) -> None:
        """Run a filter requested by the UI.

        Args:
            payload: A FilterParams instance.
        """
        if not isinstance(payload, FilterParams):
            logger.error("file_filter.bad_payload — type=%s", type(payload).__name__)
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
            logger.warning("file_filter.timeout — after %ds", DEFAULT_EXECUTION_TIMEOUT_SECONDS)
            await event_bus.publish(
                EVENT_ERROR,
                f"The operation exceeded {DEFAULT_EXECUTION_TIMEOUT_SECONDS}s and was stopped.",
            )
        except asyncio.CancelledError:
            # This handler task is itself the cancellation target and the cancel
            # handler has already told the UI, so a deliberate user action is
            # kept out of the error log.
            logger.info("file_filter.cancelled")
        except Exception as exc:
            logger.error("file_filter.execute_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    async def _on_undo(self, payload: Any) -> None:
        """Reverse a completed Move run at the UI's request.

        Args:
            payload: An UndoParams instance.
        """
        if not isinstance(payload, UndoParams):
            logger.error("file_filter.bad_undo_payload — type=%s", type(payload).__name__)
            return
        try:
            result = await run_in_thread(self.undo, payload)
            self._last_result = result
            await event_bus.publish(EVENT_DONE, result)
        except Exception as exc:
            logger.error("file_filter.undo_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    def undo(self, params: UndoParams) -> FilterResult:
        """Move every relocated file back where it came from.

        Args:
            params: The pairs to reverse.

        Returns:
            A result describing the reversal.

        Raises:
            ValueError: When nothing could be moved back.
        """
        restored = 0
        warnings: list[str] = []

        for pair in params.pairs:
            if not pair.destination_path.exists():
                warnings.append(f"{pair.destination_path.name}: no longer exists")
                continue
            if pair.source_path.exists():
                warnings.append(
                    f"{pair.source_path.name}: something is already back at the original path"
                )
                continue
            try:
                pair.source_path.parent.mkdir(parents=True, exist_ok=True)
                safe_move(pair.destination_path, pair.source_path)
                restored += 1
            except OSError as exc:
                warnings.append(f"{pair.destination_path.name}: {exc}")

        if restored == 0:
            detail = "Nothing could be moved back."
            raise ValueError(
                detail + " " + "; ".join(warnings[:3]) if warnings else detail
            )

        logger.info(
            "file_filter.undone — restored=%d skipped=%d", restored, len(warnings)
        )
        noun = "file" if restored == 1 else "files"
        return FilterResult(
            output_mode=OutputMode.MOVE,
            files_written=restored,
            detail=f"Moved {restored} {noun} back to the source tree.",
            warnings=warnings,
        )

    # ─── Scanning ─────────────────────────────────────────────────────────────

    def scan(
        self,
        params: ScanParams,
        on_progress: Callable[[int], None] | None = None,
    ) -> ScanResult:
        """Count files by extension across a directory tree.

        Args:
            params: Scan parameters.

        Returns:
            Per-extension counts, largest first.
        """
        counts: dict[str, int] = defaultdict(int)
        sizes: dict[str, int] = defaultdict(int)
        total_files = 0
        total_bytes = 0

        for path in self.walk(params.source_dir, params.exclude_patterns):
            extension = self.extension_of(path)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            counts[extension] += 1
            sizes[extension] += size
            total_files += 1
            total_bytes += size
            if on_progress is not None and total_files % SCAN_REPORT_EVERY == 0:
                on_progress(total_files)

        extensions = [
            ExtensionCount(extension=name, count=count, total_bytes=sizes[name])
            for name, count in counts.items()
        ]
        # Most numerous first; ties broken alphabetically so the list is stable.
        extensions.sort(key=lambda item: (-item.count, item.extension))

        logger.info(
            "file_filter.scanned — dir=%s files=%d extensions=%d",
            params.source_dir,
            total_files,
            len(extensions),
        )
        return ScanResult(
            source_dir=params.source_dir,
            extensions=extensions,
            total_files=total_files,
            total_bytes=total_bytes,
        )

    def walk(self, root: Path, exclude_patterns: list[str]) -> list[Path]:
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

    def extension_of(self, path: Path) -> str:
        """Return a file's extension label, lowercased.

        Args:
            path: The file.

        Returns:
            The dot-prefixed suffix, or the no-extension label.
        """
        return path.suffix.lower() if path.suffix else NO_EXTENSION_LABEL

    def select(self, params: FilterParams) -> list[Path]:
        """List the files a filter run would act on.

        Args:
            params: Filter parameters.

        Returns:
            Matching files, sorted.
        """
        wanted = {e.lower() for e in params.extensions}
        candidates = self.walk(params.source_dir, params.exclude_patterns)
        if not wanted:
            return candidates
        return [p for p in candidates if self.extension_of(p) in wanted]

    # ─── Execution ────────────────────────────────────────────────────────────

    async def execute(self, params: FilterParams) -> AsyncIterator[ProgressEvent]:
        """Extract the selected files in the chosen mode.

        Args:
            params: Validated filter parameters.

        Yields:
            ProgressEvent at each checkpoint.
        """
        yield ProgressEvent(percent=PROGRESS_START, message="Scanning…")

        selected = await run_in_thread(self.select, params)
        if not selected:
            raise ValueError(
                "No files matched. Check the selected extensions and exclude patterns."
            )

        total_bytes = sum(self._safe_size(p) for p in selected)
        yield ProgressEvent(
            percent=PROGRESS_SCAN_DONE,
            message=f"Matched {len(selected)} files. Preparing {params.output_mode.value}…",
        )

        # Rule B-07 — confine writes to exports/, temp/, or the directory the
        # user chose for this run. Resolving here also stops a crafted filename
        # from escaping that directory via traversal.
        output_dir = validate_write_target(
            params.output_dir / OUTPUT_SUBDIR, extra_roots=(params.output_dir,)
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime(TIMESTAMP_FORMAT)
        stem = params.source_dir.name or "root"

        if params.output_mode is OutputMode.MANIFEST:
            outputs, written, warnings, moved = await run_in_thread(
                self._write_manifest, selected, params, output_dir, stem, timestamp
            )
        elif params.output_mode is OutputMode.ZIP:
            outputs, written, warnings, moved = await run_in_thread(
                self._write_archive, selected, params, output_dir, stem, timestamp
            )
        else:
            outputs, written, warnings, moved = await run_in_thread(
                self._write_files, selected, params, output_dir, stem, timestamp
            )

        yield ProgressEvent(
            percent=PROGRESS_WORK_END, message=f"Wrote {written} files."
        )

        self._last_result = FilterResult(
            output_mode=params.output_mode,
            output_paths=outputs,
            files_matched=len(selected),
            files_written=written,
            total_bytes=total_bytes,
            moved_pairs=moved,
            detail=self._summarise(params.output_mode, written, len(selected)),
            warnings=warnings,
        )
        logger.info(
            "file_filter.done — mode=%s matched=%d written=%d",
            params.output_mode.value,
            len(selected),
            written,
        )
        yield ProgressEvent(
            percent=PROGRESS_COMPLETE,
            message=self._last_result.detail,
            output_path=outputs[0] if outputs else None,
        )

    # ─── Output modes ─────────────────────────────────────────────────────────

    def _write_files(
        self,
        selected: list[Path],
        params: FilterParams,
        output_dir: Path,
        stem: str,
        timestamp: str,
    ) -> tuple[list[Path], int, list[str], list[MovedPair]]:
        """Copy or move the selected files into a new folder.

        Move *relocates* each file (a rename on the same volume) rather than
        copying it and recycling the original. The old approach needed twice the
        space — the copy plus the recycled original — and freed none of it. A
        move is not destructive, so it is made reversible by remembering the
        ``(source, destination)`` pairs, the same way ``bulk_renamer`` undoes a
        rename. See the audit's §3.6.

        Returns:
            ``(outputs, written, warnings, moved_pairs)``.
        """
        target_root = output_dir / COPY_DIR_TEMPLATE.format(
            stem=stem, timestamp=timestamp
        )
        target_root.mkdir(parents=True, exist_ok=True)

        moving = params.output_mode is OutputMode.MOVE
        warnings: list[str] = []
        written = 0
        moved: list[MovedPair] = []

        for source in selected:
            destination = target_root / self._relative_target(source, params)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if moving:
                    landed = safe_move(source, destination)
                    moved.append(
                        MovedPair(source_path=source, destination_path=landed)
                    )
                else:
                    safe_copy(source, destination)
                written += 1
            except OSError as exc:
                warnings.append(f"{source.name}: {exc}")
                logger.warning(
                    "file_filter.%s_failed — path=%s",
                    "move" if moving else "copy",
                    source,
                    exc_info=exc,
                )

        return [target_root], written, warnings, moved

    def _write_archive(
        self,
        selected: list[Path],
        params: FilterParams,
        output_dir: Path,
        stem: str,
        timestamp: str,
    ) -> tuple[list[Path], int, list[str], list[MovedPair]]:
        """Pack the selected files into a single zip archive.

        Returns:
            ``(outputs, written, warnings, moved_pairs)`` — nothing is
            relocated, so the pair list is always empty.
        """
        target = output_dir / ARCHIVE_NAME_TEMPLATE.format(
            stem=stem, timestamp=timestamp
        )
        warnings: list[str] = []
        written = 0

        with zipfile.ZipFile(
            target, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for source in selected:
                try:
                    archive.write(source, self._relative_target(source, params))
                    written += 1
                except OSError as exc:
                    warnings.append(f"{source.name}: {exc}")
                    logger.warning(
                        "file_filter.zip_failed — path=%s", source, exc_info=exc
                    )

        return [target], written, warnings, []

    def _write_manifest(
        self,
        selected: list[Path],
        params: FilterParams,
        output_dir: Path,
        stem: str,
        timestamp: str,
    ) -> tuple[list[Path], int, list[str], list[MovedPair]]:
        """Record the selection as a tab-separated listing, copying nothing.

        Returns:
            ``(outputs, written, warnings, moved_pairs)`` — nothing is
            relocated, so the pair list is always empty.
        """
        target = output_dir / MANIFEST_NAME_TEMPLATE.format(
            stem=stem, timestamp=timestamp
        )
        rows = [MANIFEST_HEADER, f"# source: {params.source_dir}", MANIFEST_COLUMNS]
        warnings: list[str] = []
        written = 0

        for source in selected:
            try:
                stat = source.stat()
            except OSError as exc:
                warnings.append(f"{source.name}: {exc}")
                continue
            rows.append(
                MANIFEST_ROW_TEMPLATE.format(
                    path=self._relative_target(source, params),
                    size=stat.st_size,
                    modified=datetime.datetime.fromtimestamp(
                        stat.st_mtime
                    ).isoformat(timespec="seconds"),
                )
            )
            written += 1

        target.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return [target], written, warnings, []

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _relative_target(self, source: Path, params: FilterParams) -> str:
        """Return the path a file should take inside the output.

        Args:
            source: The source file.
            params: Filter parameters, for the hierarchy choice.

        Returns:
            A relative path when preserving hierarchy, else the bare filename.
        """
        if not params.preserve_hierarchy:
            return source.name
        try:
            return str(source.relative_to(params.source_dir)).replace("\\", "/")
        except ValueError:
            return source.name

    def _safe_size(self, path: Path) -> int:
        """Return a file's size, or 0 when it cannot be read.

        Args:
            path: The file to measure.
        """
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _summarise(self, mode: OutputMode, written: int, matched: int) -> str:
        """Build the completion message shown in the UI.

        Args:
            mode: The output mode that ran.
            written: Files successfully handled.
            matched: Files the filter selected.

        Returns:
            A short human-readable summary.
        """
        verb = {
            OutputMode.COPY: "Copied",
            OutputMode.MOVE: "Moved",
            OutputMode.ZIP: "Archived",
            OutputMode.MANIFEST: "Listed",
        }[mode]
        noun = "file" if written == 1 else "files"
        shortfall = "" if written == matched else f" ({matched - written} skipped)"
        return f"{verb} {written} {noun}{shortfall}"
