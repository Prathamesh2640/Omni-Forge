"""PDF Suite — business logic layer.

Every operation runs through PyMuPDF, which is a pure-Python wheel with no
external binary, so the whole suite works offline (rule C-01). The one
exception is DOCX conversion via pdf2docx, also a wheel.

Zero NiceGUI imports permitted (rule A-01).
"""
from __future__ import annotations

import asyncio
import datetime
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import fitz
from pydantic import SecretStr

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.sandbox import SandboxTask, run_in_thread
from modules.converters.pdf_suite.constants import (
    COMPRESSED_FILENAME_TEMPLATE,
    DECRYPTED_FILENAME_TEMPLATE,
    DEGREES_IN_CIRCLE,
    DOCX_FILENAME_TEMPLATE,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    IMAGE_FILENAME_TEMPLATE,
    IMAGES_SUBDIR_TEMPLATE,
    MERGED_FILENAME_TEMPLATE,
    METADATA_FILENAME_TEMPLATE,
    MIN_IMAGE_PIXELS_TO_RESAMPLE,
    OUTPUT_SUBDIR,
    PRESET_IMAGE_DPI,
    PRESET_JPEG_QUALITY,
    PROGRESS_COMPLETE,
    PROGRESS_START,
    PROGRESS_VALIDATED,
    PROGRESS_WORK_END,
    PROGRESS_WORK_START,
    ROTATED_FILENAME_TEMPLATE,
    SPLIT_FILENAME_TEMPLATE,
    TEMP_DECRYPTED_TEMPLATE,
    TEXT_FILENAME_TEMPLATE,
    TIMESTAMP_FORMAT,
)
from modules.converters.pdf_suite.models import (
    PdfOperation,
    PdfParams,
    PdfResult,
    SplitMode,
)
from shared.constants import DEFAULT_EXECUTION_TIMEOUT_SECONDS, TEMP_DIR
from shared.validators import validate_write_target

logger = get_logger(__name__)


class PdfPasswordError(RuntimeError):
    """Raised when a document cannot be unlocked with the supplied password."""


class PdfSuiteLogic:
    """Implements every pdf_suite operation.

    ``execute()`` dispatches on :class:`PdfOperation` and yields
    ProgressEvents; the completed :class:`PdfResult` is recorded on
    :attr:`_last_result` and published once the generator is exhausted,
    because ProgressEvent has no room for the statistics.
    """

    def __init__(self) -> None:
        self._execution = SandboxTask()
        self._last_result: PdfResult | None = None

    async def register(self) -> None:
        """Subscribe the EventBus execute handler.  Call from ``on_load()``."""
        event_bus.subscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.subscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("pdf_suite.logic.registered")

    async def unregister(self) -> None:
        """Unsubscribe the EventBus handler.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.unsubscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("pdf_suite.logic.unregistered")

    # ─── EventBus handler ─────────────────────────────────────────────────────

    async def _on_cancel(self, _payload: Any) -> None:
        """Stop the in-flight operation at the user's request.

        The cancelled run reports nothing itself — this handler owns telling
        the UI, so the execute handler can stay quiet about a deliberate
        user action (RFC 0003).
        """
        if self._execution.request_cancel():
            logger.info("pdf_suite.cancel_requested")
            await event_bus.publish(EVENT_CANCELLED, None)

    async def _on_execute(self, payload: Any) -> None:
        """Run an operation requested by the UI.

        Args:
            payload: A PdfParams instance.
        """
        if not isinstance(payload, PdfParams):
            logger.error("pdf_suite.bad_payload — type=%s", type(payload).__name__)
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
            logger.warning("pdf_suite.timeout — after %ds", DEFAULT_EXECUTION_TIMEOUT_SECONDS)
            await event_bus.publish(
                EVENT_ERROR,
                f"The operation exceeded {DEFAULT_EXECUTION_TIMEOUT_SECONDS}s and was stopped.",
            )
        except asyncio.CancelledError:
            # This handler task is itself the cancellation target and the cancel
            # handler has already told the UI, so a deliberate user action is
            # kept out of the error log.
            logger.info("pdf_suite.cancelled")
        except Exception as exc:
            logger.error("pdf_suite.execute_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    # ─── Dispatch ─────────────────────────────────────────────────────────────

    async def execute(self, params: PdfParams) -> AsyncIterator[ProgressEvent]:
        """Run the requested operation, reporting progress throughout.

        Args:
            params: Validated operation parameters.

        Yields:
            ProgressEvent at each checkpoint.
        """
        yield ProgressEvent(percent=PROGRESS_START, message="Validating input…")

        missing = [p for p in params.input_paths if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"File not found: {missing[0]}")

        input_bytes = sum(p.stat().st_size for p in params.input_paths)
        # Rule B-07 — confine writes to exports/, temp/, or the directory the
        # user chose for this run. Resolving here also stops a crafted filename
        # from escaping that directory via traversal.
        output_dir = validate_write_target(
            params.output_dir / OUTPUT_SUBDIR, extra_roots=(params.output_dir,)
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        yield ProgressEvent(
            percent=PROGRESS_VALIDATED,
            message=f"Running {params.operation.value.replace('_', ' ')}…",
        )

        handler = self._handlers()[params.operation]
        outputs: list[Path] = []
        pages = 0

        async for percent, message, produced, page_count in self._run(
            handler, params, output_dir
        ):
            outputs = produced or outputs
            pages = page_count or pages
            yield ProgressEvent(percent=percent, message=message)

        output_bytes = sum(self._path_size(p) for p in outputs)
        self._last_result = PdfResult(
            operation=params.operation,
            output_paths=outputs,
            pages_processed=pages,
            input_bytes=input_bytes,
            output_bytes=output_bytes,
            output_file_count=sum(self._path_file_count(p) for p in outputs),
            detail=self._summarise(params.operation, outputs, pages),
        )
        logger.info(
            "pdf_suite.done — op=%s outputs=%d pages=%d",
            params.operation.value,
            len(outputs),
            pages,
        )
        yield ProgressEvent(
            percent=PROGRESS_COMPLETE,
            message=self._last_result.detail,
            output_path=outputs[0] if outputs else None,
        )

    async def _run(
        self,
        handler: Callable[[PdfParams, Path], Iterator[tuple[int, str, list[Path], int]]],
        params: PdfParams,
        output_dir: Path,
    ) -> AsyncIterator[tuple[int, str, list[Path], int]]:
        """Drive a blocking handler from a worker thread, relaying its progress.

        PyMuPDF is synchronous, so each handler is a plain generator executed
        step by step off the event loop (rule B-01).

        Args:
            handler: The operation implementation.
            params: Operation parameters.
            output_dir: Directory to write into.

        Yields:
            ``(percent, message, outputs, pages)`` tuples from the handler.
        """
        steps = handler(params, output_dir)
        while True:
            step = await run_in_thread(_next_step, steps)
            if step is None:
                return
            yield step

    def _handlers(
        self,
    ) -> dict[
        PdfOperation,
        Callable[[PdfParams, Path], Iterator[tuple[int, str, list[Path], int]]],
    ]:
        """Map each operation to its implementation."""
        return {
            PdfOperation.MERGE: self._merge,
            PdfOperation.SPLIT: self._split,
            PdfOperation.COMPRESS: self._compress,
            PdfOperation.REMOVE_PASSWORD: self._remove_password,
            PdfOperation.EDIT_METADATA: self._edit_metadata,
            PdfOperation.ROTATE: self._rotate,
            PdfOperation.EXTRACT_TEXT: self._extract_text,
            PdfOperation.EXTRACT_IMAGES: self._extract_images,
            PdfOperation.TO_DOCX: self._to_docx,
        }

    # ─── Operations ───────────────────────────────────────────────────────────

    def _merge(
        self, params: PdfParams, output_dir: Path
    ) -> Iterator[tuple[int, str, list[Path], int]]:
        """Concatenate every input document, in the order given."""
        timestamp = datetime.datetime.now().strftime(TIMESTAMP_FORMAT)
        target = output_dir / MERGED_FILENAME_TEMPLATE.format(timestamp=timestamp)
        total = len(params.input_paths)
        pages = 0

        merged = fitz.open()
        try:
            for index, source_path in enumerate(params.input_paths, start=1):
                with self._open(source_path, params.password) as source:
                    merged.insert_pdf(source)
                    pages += source.page_count
                yield (
                    self._scaled(index, total),
                    f"Merged {index}/{total}: {source_path.name}",
                    [],
                    pages,
                )
            merged.save(target, garbage=4, deflate=True)
        finally:
            merged.close()

        yield PROGRESS_WORK_END, "Writing merged document…", [target], pages

    def _split(
        self, params: PdfParams, output_dir: Path
    ) -> Iterator[tuple[int, str, list[Path], int]]:
        """Divide a document into several files."""
        source_path = params.input_paths[0]
        outputs: list[Path] = []

        with self._open(source_path, params.password) as source:
            chunks = list(self._chunk_ranges(params, source.page_count))
            total = len(chunks)
            pages = 0

            for index, (first, last) in enumerate(chunks, start=1):
                part = fitz.open()
                try:
                    part.insert_pdf(source, from_page=first, to_page=last)
                    target = output_dir / SPLIT_FILENAME_TEMPLATE.format(
                        stem=source_path.stem, index=index
                    )
                    part.save(target, garbage=4, deflate=True)
                    outputs.append(target)
                    pages += last - first + 1
                finally:
                    part.close()
                yield (
                    self._scaled(index, total),
                    f"Wrote part {index}/{total} (pages {first + 1}-{last + 1})",
                    list(outputs),
                    pages,
                )

    def _chunk_ranges(self, params: PdfParams, page_count: int) -> Iterator[tuple[int, int]]:
        """Yield 0-based inclusive page ranges for the chosen split mode.

        Args:
            params: Operation parameters.
            page_count: Number of pages in the source document.

        Yields:
            ``(first, last)`` page index pairs.

        Raises:
            ValueError: When the requested range falls outside the document.
        """
        if params.split_mode is SplitMode.PAGE_RANGE:
            assert params.page_range is not None  # guaranteed by the model
            first, last = params.page_range
            if first > page_count:
                raise ValueError(
                    f"The document has {page_count} pages; page {first} does not exist."
                )
            yield first - 1, min(last, page_count) - 1
            return

        step = 1 if params.split_mode is SplitMode.SINGLE_PAGES else params.every_n
        for start in range(0, page_count, step):
            yield start, min(start + step, page_count) - 1

    def _compress(
        self, params: PdfParams, output_dir: Path
    ) -> Iterator[tuple[int, str, list[Path], int]]:
        """Shrink a document by resampling its embedded raster images.

        Text and vector content are never touched, so the result stays sharp
        at any zoom — only photographs and scans lose resolution.
        """
        source_path = params.input_paths[0]
        target = output_dir / COMPRESSED_FILENAME_TEMPLATE.format(stem=source_path.stem)
        dpi = PRESET_IMAGE_DPI[params.preset.value]
        quality = PRESET_JPEG_QUALITY[params.preset.value]

        with self._open(source_path, params.password) as document:
            total = document.page_count
            resampled = 0
            for index in range(total):
                resampled += self._resample_page_images(document, index, dpi, quality)
                yield (
                    self._scaled(index + 1, total),
                    f"Compressed page {index + 1}/{total}",
                    [],
                    index + 1,
                )
            document.save(target, garbage=4, deflate=True, clean=True)

        yield (
            PROGRESS_WORK_END,
            f"Recompressed {resampled} images",
            [target],
            total,
        )

    def _resample_page_images(
        self, document: fitz.Document, page_index: int, dpi: int, quality: int
    ) -> int:
        """Downsample the raster images on one page.

        Args:
            document: The open document.
            page_index: 0-based page number.
            dpi: Target resolution.
            quality: JPEG quality for the replacement image.

        Returns:
            How many images were replaced.
        """
        replaced = 0
        page = document[page_index]
        for image in page.get_images(full=True):
            xref = image[0]
            try:
                pixmap = fitz.Pixmap(document, xref)
                if pixmap.width * pixmap.height < MIN_IMAGE_PIXELS_TO_RESAMPLE:
                    continue
                scale = min(1.0, dpi / max(pixmap.xres or dpi, 1))
                if scale >= 1.0:
                    continue
                shrunk = fitz.Pixmap(
                    pixmap, int(pixmap.width * scale), int(pixmap.height * scale), None
                )
                # replace_image rewrites the image object *and* its
                # /Filter, /Width, /Height so the smaller JPEG is described
                # correctly. update_stream only swaps the bytes and leaves the
                # old metadata, which makes the page render blank — see the
                # regression test test_compress_preserves_page_content.
                page.replace_image(
                    xref, stream=shrunk.tobytes("jpeg", jpg_quality=quality)
                )
                replaced += 1
            except (ValueError, RuntimeError) as exc:
                logger.debug(
                    "pdf_suite.image_skip — page=%d xref=%s reason=%s",
                    page_index,
                    xref,
                    exc,
                )
        return replaced

    def _remove_password(
        self, params: PdfParams, output_dir: Path
    ) -> Iterator[tuple[int, str, list[Path], int]]:
        """Save an unlocked copy of an encrypted document.

        The password must be supplied by the user — this decrypts a document
        they can already open, it does not break encryption.
        """
        source_path = params.input_paths[0]
        target = output_dir / DECRYPTED_FILENAME_TEMPLATE.format(stem=source_path.stem)

        with self._open(source_path, params.password) as document:
            pages = document.page_count
            yield PROGRESS_WORK_START, f"Unlocked {pages} pages", [], pages
            document.save(target, garbage=4, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)

        yield PROGRESS_WORK_END, "Wrote unlocked document", [target], pages

    def _edit_metadata(
        self, params: PdfParams, output_dir: Path
    ) -> Iterator[tuple[int, str, list[Path], int]]:
        """Write new document metadata, leaving untouched fields as they were."""
        source_path = params.input_paths[0]
        target = output_dir / METADATA_FILENAME_TEMPLATE.format(stem=source_path.stem)
        updates = params.metadata.as_updates()

        with self._open(source_path, params.password) as document:
            existing = dict(document.metadata or {})
            existing.update(updates)
            document.set_metadata(existing)
            pages = document.page_count
            yield (
                PROGRESS_WORK_START,
                f"Updated {len(updates)} field(s)",
                [],
                pages,
            )
            document.save(target, garbage=4, deflate=True)

        yield PROGRESS_WORK_END, "Wrote updated document", [target], pages

    def _rotate(
        self, params: PdfParams, output_dir: Path
    ) -> Iterator[tuple[int, str, list[Path], int]]:
        """Rotate every page clockwise by the requested amount."""
        source_path = params.input_paths[0]
        target = output_dir / ROTATED_FILENAME_TEMPLATE.format(stem=source_path.stem)

        with self._open(source_path, params.password) as document:
            total = document.page_count
            for index in range(total):
                page = document[index]
                page.set_rotation((page.rotation + params.rotation) % DEGREES_IN_CIRCLE)
                yield (
                    self._scaled(index + 1, total),
                    f"Rotated page {index + 1}/{total}",
                    [],
                    index + 1,
                )
            document.save(target, garbage=4, deflate=True)

        yield PROGRESS_WORK_END, f"Rotated {total} pages", [target], total

    def _extract_text(
        self, params: PdfParams, output_dir: Path
    ) -> Iterator[tuple[int, str, list[Path], int]]:
        """Write the document's text layer to a UTF-8 file."""
        source_path = params.input_paths[0]
        target = output_dir / TEXT_FILENAME_TEMPLATE.format(stem=source_path.stem)

        with self._open(source_path, params.password) as document:
            total = document.page_count
            chunks: list[str] = []
            for index in range(total):
                chunks.append(document[index].get_text())
                yield (
                    self._scaled(index + 1, total),
                    f"Read page {index + 1}/{total}",
                    [],
                    index + 1,
                )

        target.write_text("\n".join(chunks), encoding="utf-8")
        yield PROGRESS_WORK_END, f"Extracted text from {total} pages", [target], total

    def _extract_images(
        self, params: PdfParams, output_dir: Path
    ) -> Iterator[tuple[int, str, list[Path], int]]:
        """Save every embedded raster image into a per-document folder."""
        source_path = params.input_paths[0]
        target_dir = output_dir / IMAGES_SUBDIR_TEMPLATE.format(stem=source_path.stem)
        target_dir.mkdir(parents=True, exist_ok=True)
        saved = 0

        with self._open(source_path, params.password) as document:
            total = document.page_count
            for index in range(total):
                for position, image in enumerate(document[index].get_images(full=True)):
                    extracted = document.extract_image(image[0])
                    name = IMAGE_FILENAME_TEMPLATE.format(
                        page=index + 1, index=position + 1, ext=extracted["ext"]
                    )
                    (target_dir / name).write_bytes(extracted["image"])
                    saved += 1
                yield (
                    self._scaled(index + 1, total),
                    f"Page {index + 1}/{total} — {saved} images so far",
                    [],
                    index + 1,
                )

        yield PROGRESS_WORK_END, f"Saved {saved} images", [target_dir], total

    def _to_docx(
        self, params: PdfParams, output_dir: Path
    ) -> Iterator[tuple[int, str, list[Path], int]]:
        """Convert the document to Word format via pdf2docx.

        pdf2docx cannot open an encrypted PDF, so an encrypted source is first
        decrypted into a temporary copy — using the password the user already
        supplied and that every other operation here honours. Without this,
        TO_DOCX was the one operation that failed on a document the rest of the
        suite handled fine.

        That copy is written to ``temp/``, never to the user's chosen output
        directory. It is a fully decrypted rendering of a document the user
        deliberately protected, so a crash between writing it and deleting it
        must not leave it sitting next to the result where it would be mistaken
        for an intended output (rule C-03).
        """
        from pdf2docx import Converter

        source_path = params.input_paths[0]
        target = output_dir / DOCX_FILENAME_TEMPLATE.format(stem=source_path.stem)
        scratch: Path | None = None

        with self._open(source_path, params.password) as document:
            pages = document.page_count
            if document.needs_pass or document.is_encrypted:
                scratch_dir = validate_write_target(Path(TEMP_DIR))
                scratch_dir.mkdir(parents=True, exist_ok=True)
                scratch = scratch_dir / TEMP_DECRYPTED_TEMPLATE.format(
                    stem=source_path.stem
                )
                document.save(scratch, encryption=fitz.PDF_ENCRYPT_NONE)

        yield PROGRESS_WORK_START, f"Converting {pages} pages to DOCX…", [], pages
        converter = Converter(str(scratch or source_path))
        try:
            converter.convert(str(target))
        finally:
            converter.close()
            if scratch is not None:
                # The decrypted copy is a means to an end and must not linger.
                scratch.unlink(missing_ok=True)

        yield PROGRESS_WORK_END, f"Converted {pages} pages", [target], pages

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _open(self, path: Path, password: SecretStr) -> fitz.Document:
        """Open a PDF, unlocking it when necessary.

        Taking the ``SecretStr`` and unwrapping it here — rather than at each of
        the nine call sites — keeps the plaintext confined to this one function.

        Args:
            path: Document to open.
            password: Password to try when the document is encrypted.

        Returns:
            The opened document.

        Raises:
            PdfPasswordError: When the document is encrypted and the password
                is missing or wrong.
        """
        document = fitz.open(path)
        if not document.needs_pass:
            return document
        if document.authenticate(password.get_secret_value()):
            return document
        document.close()
        raise PdfPasswordError(
            f"{path.name} is password-protected. Enter the correct password to continue."
        )

    def _scaled(self, done: int, total: int) -> int:
        """Map completion of *done* of *total* onto the working progress band.

        Args:
            done: Units finished.
            total: Units in the operation.

        Returns:
            A percentage between PROGRESS_WORK_START and PROGRESS_WORK_END.
        """
        if total <= 0:
            return PROGRESS_WORK_END
        span = PROGRESS_WORK_END - PROGRESS_WORK_START
        return PROGRESS_WORK_START + int(done / total * span)

    def _path_size(self, path: Path) -> int:
        """Return the size of a file, or of every file in a directory.

        Args:
            path: File or directory to measure.

        Returns:
            Size in bytes; 0 when the path is absent.
        """
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return 0

    def _path_file_count(self, path: Path) -> int:
        """Count the files a produced path represents.

        Args:
            path: A produced file or directory.

        Returns:
            1 for a file, the number of contained files for a directory, 0 when
            the path is absent.
        """
        if path.is_file():
            return 1
        if path.is_dir():
            return sum(1 for entry in path.rglob("*") if entry.is_file())
        return 0

    def _summarise(
        self, operation: PdfOperation, outputs: list[Path], pages: int
    ) -> str:
        """Build the completion message shown in the UI.

        Args:
            operation: The operation that ran.
            outputs: Files or folders produced.
            pages: Pages processed.

        Returns:
            A short human-readable summary.
        """
        noun = "file" if len(outputs) == 1 else "files"
        return (
            f"{operation.value.replace('_', ' ').title()} complete — "
            f"{pages} page{'s' if pages != 1 else ''} → {len(outputs)} {noun}"
        )


def _next_step(
    steps: Iterator[tuple[int, str, list[Path], int]],
) -> tuple[int, str, list[Path], int] | None:
    """Advance a handler generator by one step, inside a worker thread.

    Args:
        steps: The handler's generator.

    Returns:
        The next step, or ``None`` when the handler is finished.
    """
    return next(steps, None)
