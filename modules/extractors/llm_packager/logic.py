"""LLM Packager — business logic layer.

Responsibilities:
- Scan a directory for matching source files using pathspec.
- Read file contents (skip files over MAX_FILE_SIZE_BYTES).
- Concatenate into a single output file with structured separators.
- Count tokens via tiktoken.
- Yield ProgressEvents at each stage.

Zero NiceGUI imports permitted (rule A-01).
"""
from __future__ import annotations

import asyncio
import datetime
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pathspec
import tiktoken

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.sandbox import SandboxTask, run_in_thread
from modules.extractors.llm_packager.constants import (
    APPROX_CHARS_PER_TOKEN,
    CHUNK_FILE_TEMPLATE,
    CHUNK_OVERSIZE_NOTE,
    DEFAULT_TOKEN_MODEL,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    MAX_FILE_SIZE_BYTES,
    OUTPUT_FILE_TEMPLATE,
    PROGRESS_READ_DONE,
    PROGRESS_SCAN_DONE,
    PROGRESS_SCAN_START,
    PROGRESS_TOKEN_DONE,
    PROGRESS_WRITE_DONE,
    TIKTOKEN_CACHE_DIRNAME,
    TIKTOKEN_CACHE_ENV_VARS,
    TOC_HEADER,
    TOC_ROW_TEMPLATE,
    TOC_SEPARATOR_WIDTH,
    TOKEN_MODELS,
)
from modules.extractors.llm_packager.models import (
    PackagedFile,
    PackageParams,
    PackageResult,
)
from shared.constants import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from shared.validators import validate_write_target

logger = get_logger(__name__)

_FILE_HEADER = "=" * 80
_SEPARATOR_BLANK_LINES = "\n\n"


def tiktoken_cache_dir() -> Path:
    """Return the directory tiktoken uses for its downloaded vocabulary.

    Mirrors tiktoken's own resolution order so the check reflects what the
    library will actually find.

    Returns:
        The effective cache directory, whether or not it exists.
    """
    for variable in TIKTOKEN_CACHE_ENV_VARS:
        override = os.environ.get(variable)
        if override:
            return Path(override)
    return Path(tempfile.gettempdir()) / TIKTOKEN_CACHE_DIRNAME


def vocabulary_is_available() -> bool:
    """Return True when a tiktoken vocabulary is already cached locally.

    Used to decide whether exact token counting is possible offline. When
    this is False the module estimates rather than triggering a download
    (rule C-01).

    Returns:
        True when the cache directory holds at least one blob.
    """
    cache = tiktoken_cache_dir()
    try:
        return cache.is_dir() and any(entry.is_file() for entry in cache.iterdir())
    except OSError as exc:
        logger.debug("llm_packager.cache_probe_failed — reason=%s", exc)
        return False


class LLMPackagerLogic:
    """Handles file scanning, token counting, and output generation.

    ``execute()`` yields ProgressEvents, which carry no room for run
    statistics. The full :class:`PackageResult` of the most recent run is
    therefore recorded on :attr:`_last_result` and published separately once
    the generator is exhausted.
    """

    def __init__(self) -> None:
        self._execution = SandboxTask()
        self._last_result: PackageResult | None = None

    async def register(self) -> None:
        """Subscribe EventBus execute handler.  Call once from ``on_load()``."""
        event_bus.subscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.subscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("llm_packager.logic.registered")

    async def unregister(self) -> None:
        """Unsubscribe EventBus handler.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.unsubscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("llm_packager.logic.unregistered")

    # ─── EventBus handler ─────────────────────────────────────────────────────

    async def _on_cancel(self, _payload: Any) -> None:
        """Stop the in-flight operation at the user's request.

        The cancelled run reports nothing itself — this handler owns telling
        the UI, so the execute handler can stay quiet about a deliberate
        user action (RFC 0003).
        """
        if self._execution.request_cancel():
            logger.info("llm_packager.cancel_requested")
            await event_bus.publish(EVENT_CANCELLED, None)

    async def _on_execute(self, payload: Any) -> None:
        """Handle execute request from the UI.

        Args:
            payload: PackageParams instance.
        """
        if not isinstance(payload, PackageParams):
            logger.error("llm_packager.bad_payload — type=%s", type(payload).__name__)
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
            logger.warning("llm_packager.timeout — after %ds", DEFAULT_EXECUTION_TIMEOUT_SECONDS)
            await event_bus.publish(
                EVENT_ERROR,
                f"The operation exceeded {DEFAULT_EXECUTION_TIMEOUT_SECONDS}s and was stopped.",
            )
        except asyncio.CancelledError:
            # This handler task is itself the cancellation target and the cancel
            # handler has already told the UI, so a deliberate user action is
            # kept out of the error log.
            logger.info("llm_packager.cancelled")
        except Exception as exc:
            logger.error("llm_packager.execute_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    # ─── Core execute() ───────────────────────────────────────────────────────

    async def execute(self, params: PackageParams) -> AsyncIterator[ProgressEvent]:
        """Scan, read, pack, and count tokens for a codebase directory.

        Yields ProgressEvents at 5 major checkpoints (rule D-08 satisfied
        because each read batch emits intermediate events too).

        Args:
            params: Validated PackageParams.

        Yields:
            ProgressEvent at each stage.
        """
        # 1 — Scan
        yield ProgressEvent(percent=PROGRESS_SCAN_START, message="Scanning files…")
        matched_files = await run_in_thread(
            self._scan_files, params.source_dir, params.extensions, params.exclude_patterns
        )
        yield ProgressEvent(
            percent=PROGRESS_SCAN_DONE,
            message=f"Found {len(matched_files)} files. Reading…",
        )

        # 2 — Read files
        packaged: list[PackagedFile] = []
        skipped: int = 0
        total_files = len(matched_files)

        for idx, fpath in enumerate(matched_files):
            text = await run_in_thread(self._safe_read, fpath)
            if text is None:
                skipped += 1
            else:
                rel = fpath.relative_to(params.source_dir)
                packaged.append(
                    PackagedFile(
                        relative_path=str(rel).replace("\\", "/"),
                        content=text,
                        lines=text.count("\n") + 1,
                    )
                )

            # Emit intermediate progress between SCAN_DONE and READ_DONE
            if total_files > 0:
                read_pct = PROGRESS_SCAN_DONE + int(
                    (idx + 1) / total_files * (PROGRESS_READ_DONE - PROGRESS_SCAN_DONE)
                )
                yield ProgressEvent(
                    percent=read_pct,
                    message=f"Reading {idx + 1}/{total_files}…",
                )

        yield ProgressEvent(
            percent=PROGRESS_READ_DONE,
            message=f"Read {len(packaged)} files ({skipped} skipped). Counting tokens…",
        )

        # 3 — Token count, per file so chunking and the TOC can use it
        approximate = False
        for entry in packaged:
            entry.tokens, approximate = await run_in_thread(
                self._count_tokens, self._render_file(entry), params.token_model
            )

        token_count = sum(entry.tokens for entry in packaged)
        prefix = "≈" if approximate else ""
        yield ProgressEvent(
            percent=PROGRESS_TOKEN_DONE,
            message=f"Token count: {prefix}{token_count:,}. Writing output…",
        )

        # 4 — Group into chunks and write
        groups, warnings = self._chunk(packaged, params.max_tokens_per_chunk)
        output_paths = await run_in_thread(
            self._write_chunks, groups, params, approximate
        )

        total_chars = sum(len(entry.content) for entry in packaged)
        self._last_result = PackageResult(
            output_path=output_paths[0],
            output_paths=output_paths,
            file_count=len(packaged),
            total_chars=total_chars,
            token_count=token_count,
            token_model=params.token_model,
            skipped_count=skipped,
            token_count_is_approximate=approximate,
            warnings=warnings,
        )
        logger.info(
            "llm_packager.done — files=%d tokens=%d chunks=%d",
            len(packaged),
            token_count,
            len(output_paths),
        )
        model_label = (
            "estimated"
            if approximate
            else TOKEN_MODELS.get(params.token_model, params.token_model)
        )
        chunk_note = (
            f" across {len(output_paths)} files" if len(output_paths) > 1 else ""
        )
        yield ProgressEvent(
            percent=PROGRESS_WRITE_DONE,
            message=(
                f"Done! {len(packaged)} files · {prefix}{token_count:,} tokens"
                f" ({model_label}){chunk_note}"
            ),
            output_path=output_paths[0],
        )

    # ─── Assembly ─────────────────────────────────────────────────────────────

    def _render_file(self, entry: PackagedFile) -> str:
        """Render one file's section of the output.

        Args:
            entry: The packaged file.

        Returns:
            The delimited block written into the context file.
        """
        return (
            f"{_FILE_HEADER}\n# FILE: {entry.relative_path}\n"
            f"{_FILE_HEADER}\n\n{entry.content}"
        )

    def build_toc(self, entries: list[PackagedFile], approximate: bool) -> str:
        """Build the table of contents listing every included file.

        Args:
            entries: Files in this chunk, in output order.
            approximate: Whether token counts are estimates.

        Returns:
            The rendered TOC block, ending with a blank line.
        """
        rule = "=" * TOC_SEPARATOR_WIDTH
        prefix = "≈" if approximate else ""
        rows = [
            TOC_ROW_TEMPLATE.format(
                index=index,
                path=entry.relative_path,
                lines=f"{entry.lines:,}",
                tokens=f"{prefix}{entry.tokens:,}",
            )
            for index, entry in enumerate(entries, start=1)
        ]
        totals = (
            f"{len(entries)} files · "
            f"{sum(e.lines for e in entries):,} lines · "
            f"{prefix}{sum(e.tokens for e in entries):,} tokens"
        )
        return "\n".join([rule, TOC_HEADER, rule, *rows, "", totals, rule, "", ""])

    def _chunk(
        self, entries: list[PackagedFile], limit: int
    ) -> tuple[list[list[PackagedFile]], list[str]]:
        """Group files so each chunk stays under the token limit.

        Files are never split mid-content, since half a source file is worse
        than useless to a model. A single file over the limit is emitted alone
        and reported.

        Args:
            entries: Every packaged file, in order.
            limit: Maximum tokens per chunk; 0 disables chunking.

        Returns:
            The groups, and any warnings raised while grouping.
        """
        if limit <= 0 or not entries:
            return [entries], []

        groups: list[list[PackagedFile]] = []
        current: list[PackagedFile] = []
        running = 0
        warnings: list[str] = []

        for entry in entries:
            if entry.tokens > limit:
                if current:
                    groups.append(current)
                    current, running = [], 0
                groups.append([entry])
                warnings.append(
                    CHUNK_OVERSIZE_NOTE.format(
                        path=entry.relative_path, tokens=entry.tokens, limit=limit
                    )
                )
                continue

            if current and running + entry.tokens > limit:
                groups.append(current)
                current, running = [], 0

            current.append(entry)
            running += entry.tokens

        if current:
            groups.append(current)
        return groups, warnings

    def _write_chunks(
        self,
        groups: list[list[PackagedFile]],
        params: PackageParams,
        approximate: bool,
    ) -> list[Path]:
        """Write each group to its own file.

        Args:
            groups: Files grouped per output file.
            params: The run parameters.
            approximate: Whether token counts are estimates.

        Returns:
            The paths written, in order.
        """
        # Rule B-07 — confine writes to exports/, temp/, or the user's choice.
        output_dir = validate_write_target(
            params.output_dir, extra_roots=(params.output_dir,)
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        total = len(groups)
        written: list[Path] = []

        for index, group in enumerate(groups, start=1):
            name = (
                OUTPUT_FILE_TEMPLATE.format(timestamp=timestamp)
                if total == 1
                else CHUNK_FILE_TEMPLATE.format(
                    timestamp=timestamp, index=index, total=total
                )
            )
            body = _SEPARATOR_BLANK_LINES.join(self._render_file(e) for e in group)
            header = self.build_toc(group, approximate) if params.include_toc else ""

            target = output_dir / name
            target.write_text(header + body, encoding="utf-8")
            written.append(target)

        return written

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _scan_files(
        self,
        source_dir: Path,
        extensions: list[str],
        exclude_patterns: list[str],
    ) -> list[Path]:
        """Recursively scan *source_dir* for files matching *extensions*.

        Files matching any *exclude_patterns* glob are excluded.

        Args:
            source_dir: Root directory to scan.
            extensions: Set of dot-prefixed extensions to include.
            exclude_patterns: Gitignore-style exclude patterns.

        Returns:
            Sorted list of absolute paths to matching files.
        """
        ext_set = frozenset(ext.lower() for ext in extensions)
        spec = pathspec.PathSpec.from_lines("gitignore", exclude_patterns)

        matched: list[Path] = []
        for fpath in source_dir.rglob("*"):
            if not fpath.is_file():
                continue
            try:
                rel = fpath.relative_to(source_dir)
            except ValueError:
                continue
            if spec.match_file(str(rel).replace("\\", "/")):
                continue
            if fpath.suffix.lower() not in ext_set:
                continue
            matched.append(fpath)

        matched.sort()
        return matched

    def _safe_read(self, fpath: Path) -> str | None:
        """Read *fpath* as UTF-8 text, returning None on any failure.

        Files exceeding MAX_FILE_SIZE_BYTES are also skipped.

        Args:
            fpath: File to read.

        Returns:
            File content string, or ``None`` if unreadable or too large.
        """
        try:
            size = fpath.stat().st_size
            if size > MAX_FILE_SIZE_BYTES:
                logger.debug("llm_packager.skip_large — path=%s size=%d", fpath, size)
                return None
            return fpath.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as exc:
            logger.debug("llm_packager.skip_unreadable — path=%s reason=%s", fpath, exc)
            return None

    def _count_tokens(self, text: str, model: str) -> tuple[int, bool]:
        """Count tokens in *text*, without ever reaching the network.

        ``tiktoken.get_encoding`` silently downloads its BPE vocabulary when
        it is not already cached — an external call (rule C-01) that also
        stalls the UI for tens of seconds. This checks for a local cache
        first and estimates from character count when none exists.

        Args:
            text: The full context string to tokenise.
            model: Tiktoken encoding name.

        Returns:
            A ``(count, is_approximate)`` pair. ``is_approximate`` is True
            when the count came from the character-ratio estimate.
        """
        if not vocabulary_is_available():
            logger.info("llm_packager.token_estimate — no local tiktoken vocabulary")
            return self._approximate_tokens(text), True

        try:
            enc = tiktoken.get_encoding(model)
        except Exception as exc:
            logger.warning(
                "llm_packager.tiktoken_fallback — model=%s reason=%s", model, exc
            )
            try:
                enc = tiktoken.get_encoding(DEFAULT_TOKEN_MODEL)
            except Exception as fallback_exc:
                logger.warning(
                    "llm_packager.tiktoken_unavailable — reason=%s", fallback_exc
                )
                return self._approximate_tokens(text), True
        return len(enc.encode(text)), False

    def _approximate_tokens(self, text: str) -> int:
        """Estimate a token count from character length.

        Args:
            text: The context string.

        Returns:
            Approximate token count.
        """
        return len(text) // APPROX_CHARS_PER_TOKEN

    def _write_output(self, content: str, output_dir: Path) -> Path:
        """Write *content* to a timestamped file in *output_dir*.

        Args:
            content: Full context string to write.
            output_dir: Directory for the output file.

        Returns:
            Absolute path to the written file.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = OUTPUT_FILE_TEMPLATE.format(timestamp=ts)
        out_path = output_dir / filename
        out_path.write_text(content, encoding="utf-8")
        return out_path
