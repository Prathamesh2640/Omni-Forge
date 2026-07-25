"""PDF Suite — NiceGUI presentation layer.

Renders the operation picker, file list and per-operation options, then
publishes PdfParams on the EventBus. It never imports logic.py (rule A-03).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import ui
from pydantic import SecretStr

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.storage import store_get, store_set
from modules.converters.pdf_suite.constants import (
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    FILE_LIST_HEIGHT_PX,
    PDF_EXTENSIONS,
    PDF_FILE_TYPE_LABEL,
    STORAGE_KEY_LAST_DIR,
    STORAGE_KEY_LAST_OPERATION,
    STORAGE_KEY_OUTPUT_DIR,
    STORAGE_TABLE,
)
from modules.converters.pdf_suite.models import (
    CompressPreset,
    PdfMetadata,
    PdfOperation,
    PdfParams,
    PdfResult,
    SplitMode,
)
from shared.constants import (
    COLOR_OVERLAY,
    COLOR_POSITIVE,
    COLOR_PRIMARY,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_WARNING,
)
from shared.formatters import format_bytes
from shared.ui_components import (
    ErrorPanel,
    OutputDirectoryPicker,
    ProgressPanel,
    choose_files,
    module_header,
    output_file_actions,
    section_card,
    section_title,
    stat_chip,
)

logger = get_logger(__name__)

#: Operation picker labels, in the order they appear.
OPERATION_LABELS: dict[str, str] = {
    PdfOperation.MERGE.value: "Merge — combine several PDFs into one",
    PdfOperation.SPLIT.value: "Split — divide into multiple files",
    PdfOperation.COMPRESS.value: "Compress — shrink embedded images",
    PdfOperation.REMOVE_PASSWORD.value: "Remove Password — save an unlocked copy",
    PdfOperation.EDIT_METADATA.value: "Edit Metadata — title, author, keywords",
    PdfOperation.ROTATE.value: "Rotate — turn every page",
    PdfOperation.EXTRACT_TEXT.value: "Extract Text — write the text layer to .txt",
    PdfOperation.EXTRACT_IMAGES.value: "Extract Images — save embedded pictures",
    PdfOperation.TO_DOCX.value: "Convert to DOCX — editable Word document",
}

_HELP_TEXT = (
    "Pick an operation, add one or more PDFs, then run it. Everything happens "
    "on this machine — no file ever leaves your computer. Results are written "
    "to exports/pdf_suite."
)

#: Operations that accept more than one input file.
_MULTI_FILE_OPERATIONS = frozenset({PdfOperation.MERGE})


class PdfSuiteUI:
    """Renders the PDF Suite panel and handles EventBus updates.

    Args:
        module_id: The module's dot-separated ID (used for logging).
    """

    def __init__(self, module_id: str) -> None:
        self._module_id = module_id
        self._is_running = False
        self._files: list[Path] = []

        stored_operation = str(
            store_get(STORAGE_TABLE, STORAGE_KEY_LAST_OPERATION, default=PdfOperation.MERGE.value)
        )
        self._operation = _coerce_operation(stored_operation)
        self._last_dir = str(store_get(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, default=""))

        # Option state
        self._split_mode = SplitMode.EVERY_N
        self._every_n = 1
        self._range_first = 1
        self._range_last = 1
        self._preset = CompressPreset.EBOOK
        self._password = ""
        self._rotation = 90
        self._metadata = PdfMetadata()

        # Element refs
        self._file_list: ui.column | None = None
        self._options: ui.column | None = None
        self._run_btn: ui.button | None = None
        self._result_card: ui.card | None = None
        self._output = OutputDirectoryPicker(
            initial=str(store_get(STORAGE_TABLE, STORAGE_KEY_OUTPUT_DIR, default="")),
            on_change=lambda value: store_set(
                STORAGE_TABLE, STORAGE_KEY_OUTPUT_DIR, value
            ),
        )
        self._progress = ProgressPanel()
        self._errors = ErrorPanel()

    def subscribe(self) -> None:
        """Register EventBus handlers.  Call once from ``on_load()``."""
        event_bus.subscribe(EVENT_PROGRESS, self._on_progress)
        event_bus.subscribe(EVENT_DONE, self._on_done)
        event_bus.subscribe(EVENT_ERROR, self._on_error)
        event_bus.subscribe(EVENT_CANCELLED, self._on_cancelled)

    def unsubscribe(self) -> None:
        """Deregister EventBus handlers.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_PROGRESS, self._on_progress)
        event_bus.unsubscribe(EVENT_DONE, self._on_done)
        event_bus.unsubscribe(EVENT_ERROR, self._on_error)
        event_bus.unsubscribe(EVENT_CANCELLED, self._on_cancelled)

    # ─── Render ───────────────────────────────────────────────────────────────

    def render(self, container: Any) -> None:
        """Build the PDF Suite UI inside *container*.

        Args:
            container: NiceGUI parent element.
        """
        with ui.column().style("gap: 20px; width: 100%; max-width: 860px;"):
            module_header(
                "PDF Suite",
                "Merge, split, compress and convert PDFs — fully offline.",
                _HELP_TEXT,
            )
            self._render_operation_card()
            self._render_files_card()
            with section_card():
                section_title("Destination")
                self._output.render()
            self._progress.render(on_cancel=self._on_cancel_click)
            self._errors.render()
            self._render_result_card()

    def _render_operation_card(self) -> None:
        """Render the operation picker and its dependent options."""
        with section_card():
            section_title("Operation")
            ui.select(
                options=OPERATION_LABELS,
                value=self._operation.value,
                on_change=self._on_operation_change,
            ).props("outlined dense options-dense").style("width: 100%;")

            self._options = ui.column().style(
                "gap: 12px; width: 100%; margin-top: 14px;"
            )
            self._render_options()

    def _render_options(self) -> None:
        """Repaint the options that apply to the selected operation."""
        if self._options is None:
            return
        self._options.clear()
        with self._options:
            if self._operation is PdfOperation.SPLIT:
                self._render_split_options()
            elif self._operation is PdfOperation.COMPRESS:
                self._render_compress_options()
            elif self._operation is PdfOperation.EDIT_METADATA:
                self._render_metadata_options()
            elif self._operation is PdfOperation.ROTATE:
                self._render_rotate_options()

            # Any document may be encrypted, so the password applies throughout.
            ui.input(
                label="Password (only if the PDF is protected)",
                password=True,
                password_toggle_button=True,
                on_change=lambda e: setattr(self, "_password", e.value or ""),
            ).props("outlined dense").style("width: 100%;")

    def _render_split_options(self) -> None:
        """Render the split-mode controls."""
        ui.select(
            options={
                SplitMode.EVERY_N.value: "Every N pages",
                SplitMode.SINGLE_PAGES.value: "One file per page",
                SplitMode.PAGE_RANGE.value: "A single page range",
            },
            value=self._split_mode.value,
            label="Split mode",
            on_change=self._on_split_mode_change,
        ).props("outlined dense").style("width: 100%;")

        if self._split_mode is SplitMode.EVERY_N:
            ui.number(
                label="Pages per file",
                value=self._every_n,
                min=1,
                precision=0,
                on_change=lambda e: setattr(self, "_every_n", int(e.value or 1)),
            ).props("outlined dense").style("width: 100%;")
        elif self._split_mode is SplitMode.PAGE_RANGE:
            with ui.row().style("gap: 10px; width: 100%;"):
                ui.number(
                    label="First page",
                    value=self._range_first,
                    min=1,
                    precision=0,
                    on_change=lambda e: setattr(self, "_range_first", int(e.value or 1)),
                ).props("outlined dense").style("flex: 1;")
                ui.number(
                    label="Last page",
                    value=self._range_last,
                    min=1,
                    precision=0,
                    on_change=lambda e: setattr(self, "_range_last", int(e.value or 1)),
                ).props("outlined dense").style("flex: 1;")

    def _render_compress_options(self) -> None:
        """Render the compression preset picker."""
        ui.select(
            options={
                CompressPreset.SCREEN.value: "Screen — smallest, 72 DPI images",
                CompressPreset.EBOOK.value: "eBook — balanced, 150 DPI images",
                CompressPreset.PRINT.value: "Print — highest quality, 300 DPI images",
            },
            value=self._preset.value,
            label="Quality preset",
            on_change=lambda e: setattr(self, "_preset", CompressPreset(str(e.value))),
        ).props("outlined dense").style("width: 100%;")
        ui.label(
            "Only embedded photographs and scans are resampled — text and "
            "vector graphics stay sharp at any zoom."
        ).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")

    def _render_metadata_options(self) -> None:
        """Render the editable metadata fields."""
        for field in ("title", "author", "subject", "keywords"):
            ui.input(
                label=field.title(),
                on_change=lambda e, f=field: setattr(
                    self._metadata, f, (e.value or None)
                ),
            ).props("outlined dense").style("width: 100%;")

    def _render_rotate_options(self) -> None:
        """Render the rotation picker."""
        ui.select(
            options={90: "90° clockwise", 180: "180°", 270: "270° clockwise"},
            value=self._rotation,
            label="Rotation",
            on_change=lambda e: setattr(self, "_rotation", int(e.value)),
        ).props("outlined dense").style("width: 100%;")

    def _render_files_card(self) -> None:
        """Render the input file list and its controls."""
        with section_card():
            with ui.row().style(
                "align-items: center; justify-content: space-between; width: 100%;"
            ):
                section_title("Input Files")
                with ui.row().style("gap: 8px;"):
                    ui.button("Add PDFs…", on_click=self._on_add_files).props(
                        "flat dense no-caps"
                    ).style(f"color: {COLOR_TEXT_MUTED};")
                    ui.button("Clear", on_click=self._on_clear_files).props(
                        "flat dense no-caps"
                    ).style(f"color: {COLOR_TEXT_MUTED};")

            self._file_list = ui.column().style(
                f"gap: 4px; width: 100%; max-height: {FILE_LIST_HEIGHT_PX}px;"
                " overflow-y: auto;"
            )
            self._refresh_file_list()

            with ui.row().style("justify-content: flex-end; width: 100%; margin-top: 14px;"):
                self._run_btn = ui.button("Run", on_click=self._on_run_click).props(
                    "no-caps"
                ).style(
                    f"background: {COLOR_PRIMARY}; color: white; font-weight: 600;"
                    " padding: 8px 24px; border-radius: 6px;"
                )

    def _refresh_file_list(self) -> None:
        """Repaint the selected-file rows."""
        if self._file_list is None:
            return
        self._file_list.clear()
        with self._file_list:
            if not self._files:
                ui.label("No files selected yet.").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 10px 2px;"
                )
                return
            for index, path in enumerate(self._files):
                self._render_file_row(index, path)

    def _render_file_row(self, index: int, path: Path) -> None:
        """Render one file row with its reorder and remove controls.

        Args:
            index: Position in the list.
            path: The file.
        """
        with ui.row().style(
            f"align-items: center; gap: 8px; width: 100%; padding: 6px 10px;"
            f" background: {COLOR_OVERLAY}; border-radius: 6px;"
        ):
            ui.label(f"{index + 1}.").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 11px; min-width: 20px;"
            )
            ui.label(path.name).style(
                f"color: {COLOR_TEXT_MUTED}; font-size: 12px; flex: 1;"
                " font-family: monospace; word-break: break-all;"
            )
            ui.label(format_bytes(path.stat().st_size if path.is_file() else 0)).style(
                f"color: {COLOR_TEXT_DIM}; font-size: 11px;"
            )
            # Order matters for merge, so rows can be moved.
            if self._operation in _MULTI_FILE_OPERATIONS:
                ui.button(icon="arrow_upward", on_click=lambda _e, i=index: self._move(i, -1)).props(
                    "flat dense round size=sm"
                ).style(f"color: {COLOR_TEXT_DIM};")
                ui.button(icon="arrow_downward", on_click=lambda _e, i=index: self._move(i, 1)).props(
                    "flat dense round size=sm"
                ).style(f"color: {COLOR_TEXT_DIM};")
            ui.button(icon="close", on_click=lambda _e, i=index: self._remove(i)).props(
                "flat dense round size=sm"
            ).style(f"color: {COLOR_TEXT_DIM};")

    def _render_result_card(self) -> None:
        """Render the (initially empty) result card."""
        with section_card() as self._result_card:
            ui.label("Results will appear here.").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 13px;"
            )

    # ─── File selection ───────────────────────────────────────────────────────

    async def _on_add_files(self) -> None:
        """Let the user pick specific PDF files and add them to the list."""
        start = Path(self._last_dir) if self._last_dir else None
        chosen = await choose_files(
            start, extensions=PDF_EXTENSIONS, label=PDF_FILE_TYPE_LABEL
        )
        if not chosen:
            return

        self._last_dir = str(chosen[0].parent)
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, self._last_dir)

        limit = None if self._operation in _MULTI_FILE_OPERATIONS else 1
        existing = {p.resolve() for p in self._files}
        added = 0
        for path in chosen:
            if path.resolve() in existing:
                continue
            if limit is not None and len(self._files) >= limit:
                break
            self._files.append(path)
            existing.add(path.resolve())
            added += 1

        if added < len(chosen) and limit is not None:
            ui.notify(
                f"{self._operation.value.replace('_', ' ')} takes one file"
                " — the rest were not added.",
                type="info",
            )
        self._refresh_file_list()

    def _on_clear_files(self) -> None:
        """Empty the file list."""
        self._files.clear()
        self._refresh_file_list()

    def _move(self, index: int, delta: int) -> None:
        """Reorder a file, which determines merge order.

        Args:
            index: Row to move.
            delta: -1 to move up, 1 to move down.
        """
        target = index + delta
        if 0 <= target < len(self._files):
            self._files[index], self._files[target] = (
                self._files[target],
                self._files[index],
            )
            self._refresh_file_list()

    def _remove(self, index: int) -> None:
        """Remove a file from the list.

        Args:
            index: Row to remove.
        """
        if 0 <= index < len(self._files):
            self._files.pop(index)
            self._refresh_file_list()

    # ─── Controls ─────────────────────────────────────────────────────────────

    def _on_operation_change(self, event: Any) -> None:
        """Switch operation, trimming the file list when it must shrink."""
        self._operation = _coerce_operation(str(event.value))
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_OPERATION, self._operation.value)
        if self._operation not in _MULTI_FILE_OPERATIONS and len(self._files) > 1:
            self._files = self._files[:1]
        self._render_options()
        self._refresh_file_list()

    def _on_split_mode_change(self, event: Any) -> None:
        """Switch split mode and repaint its dependent inputs."""
        self._split_mode = SplitMode(str(event.value))
        self._render_options()

    async def _on_run_click(self) -> None:
        """Validate the form and publish the execute request."""
        if self._is_running:
            return
        if not self._files:
            ui.notify("Add at least one PDF file.", type="warning")
            return

        try:
            params = PdfParams(
                operation=self._operation,
                input_paths=list(self._files),
                output_dir=self._output.path,
                split_mode=self._split_mode,
                page_range=(self._range_first, self._range_last),
                every_n=self._every_n,
                preset=self._preset,
                password=SecretStr(self._password),
                metadata=self._metadata,
                rotation=self._rotation,
            )
        except ValueError as exc:
            ui.notify(_first_error(exc), type="negative")
            return

        self._is_running = True
        self._errors.hide()
        self._progress.update(0, "Starting…")
        if self._run_btn is not None:
            self._run_btn.set_text("Working…")

        await event_bus.publish(EVENT_EXECUTE, params)

    # ─── EventBus handlers ────────────────────────────────────────────────────

    async def _on_progress(self, payload: Any) -> None:
        """Advance the progress panel.

        Args:
            payload: A ProgressEvent.
        """
        if isinstance(payload, ProgressEvent):
            self._progress.update(payload.percent, payload.message)

    async def _on_done(self, payload: Any) -> None:
        """Render the result card once an operation completes.

        Args:
            payload: A PdfResult.
        """
        if not isinstance(payload, PdfResult):
            return
        self._reset_controls()

        if self._result_card is None:
            return
        self._result_card.clear()
        with self._result_card:
            self._render_result(payload)


    async def _on_cancel_click(self) -> None:
        """Ask the logic layer to stop the operation currently running."""
        await event_bus.publish(EVENT_CANCEL, None)

    async def _on_cancelled(self, _payload: Any) -> None:
        """Return the panel to an idle state once a cancel takes effect."""
        self._is_running = False
        self._progress.reset("Cancelled.")

    async def _on_error(self, payload: Any) -> None:
        """Show a failure in the error panel.

        Args:
            payload: The error message.
        """
        message = str(payload)
        self._reset_controls()
        self._progress.reset("Failed.")
        self._errors.show("The operation could not be completed.", message)
        logger.error("pdf_suite.ui.error — %s", message)

    def _reset_controls(self) -> None:
        """Return the run button to its idle state."""
        self._is_running = False
        if self._run_btn is not None:
            self._run_btn.set_text("Run")

    def _render_result(self, result: PdfResult) -> None:
        """Populate the result card.

        Args:
            result: The completed operation's result.
        """
        ui.label(result.detail).style(
            f"color: {COLOR_POSITIVE}; font-size: 15px; font-weight: 700;"
            " margin-bottom: 12px;"
        )

        with ui.grid(columns=3).style("gap: 16px; width: 100%;"):
            stat_chip("📄 Pages", str(result.pages_processed))
            stat_chip("📦 Output", format_bytes(result.output_bytes))
            saved = result.bytes_saved
            stat_chip(
                "📉 Saved" if saved >= 0 else "📈 Larger",
                f"{format_bytes(abs(saved))} ({abs(result.size_change_percent):.0f}%)",
                colour=COLOR_POSITIVE if saved > 0 else COLOR_WARNING,
            )

        for path in result.output_paths:
            if path.is_dir():
                self._render_folder_row(path, result.output_file_count)
            else:
                output_file_actions(path)

    def _render_folder_row(self, path: Path, file_count: int) -> None:
        """Render a row for an output directory, which cannot be downloaded.

        Args:
            path: The produced directory.
            file_count: How many files it holds, counted by the logic layer —
                walking the tree here would do disk I/O on the event loop while
                painting the result card.
        """
        with ui.row().style(
            f"align-items: center; gap: 10px; margin-top: 14px; width: 100%;"
            f" background: {COLOR_OVERLAY}; border-radius: 6px; padding: 10px 14px;"
        ):
            ui.icon("folder").style(f"color: {COLOR_WARNING}; font-size: 18px;")
            ui.label(str(path)).style(
                f"color: {COLOR_TEXT_MUTED}; font-size: 12px;"
                " font-family: monospace; flex: 1; word-break: break-all;"
            )
            ui.label(f"{file_count} files").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 11px;"
            )


def _coerce_operation(value: str) -> PdfOperation:
    """Convert a stored string to an operation, tolerating stale values.

    Args:
        value: The persisted operation name.

    Returns:
        The matching operation, or MERGE when unrecognised.
    """
    try:
        return PdfOperation(value)
    except ValueError:
        return PdfOperation.MERGE


def _first_error(exc: Exception) -> str:
    """Extract a single readable line from a validation failure.

    Args:
        exc: The raised exception.

    Returns:
        A message suitable for a notification.
    """
    text = str(exc)
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned.startswith("Value error, "):
            return cleaned.removeprefix("Value error, ")
    return text.splitlines()[0] if text else "Invalid parameters."
