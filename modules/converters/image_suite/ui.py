"""Image Suite — NiceGUI presentation layer.

Renders the operation picker, file list and per-operation options, then
publishes ImageParams on the EventBus. It never imports logic.py (rule A-03).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import ui

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.storage import store_get, store_set
from modules.converters.image_suite.constants import (
    DEFAULT_MAX_DIMENSION,
    DEFAULT_QUALITY,
    DEFAULT_TARGET_KB,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    FILE_LIST_HEIGHT_PX,
    MAX_QUALITY,
    MIN_QUALITY,
    STORAGE_KEY_LAST_DIR,
    STORAGE_KEY_LAST_OPERATION,
    STORAGE_KEY_OUTPUT_DIR,
    STORAGE_TABLE,
)
from modules.converters.image_suite.models import (
    INPUT_EXTENSIONS,
    ImageOperation,
    ImageParams,
    ImageResult,
    OutputFormat,
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

#: Operation picker labels.
OPERATION_LABELS: dict[str, str] = {
    ImageOperation.CONVERT.value: "Convert — HEIC / WebP / PNG / JPG",
    ImageOperation.COMPRESS_TO_TARGET.value: "Compress — hit a target file size",
    ImageOperation.RESIZE.value: "Resize — cap the longest edge",
    ImageOperation.STRIP_METADATA.value: "Strip Metadata — remove EXIF, ICC, XMP",
    ImageOperation.SVG_TO_DENSITIES.value: "SVG → Android density PNGs",
    ImageOperation.SVG_TO_FAVICONS.value: "SVG → Web favicons (+ .ico)",
    ImageOperation.SVG_TO_VECTOR_DRAWABLE.value: "SVG → Android VectorDrawable",
}

_HELP_TEXT = (
    "Pick an operation, add images, then run it. Everything is processed on "
    "this machine — no image is uploaded. Results are written to "
    "exports/image_suite."
)

#: Operations that write a raster file and so offer a format choice.
_FORMAT_OPERATIONS = frozenset(
    {
        ImageOperation.CONVERT,
        ImageOperation.COMPRESS_TO_TARGET,
        ImageOperation.RESIZE,
        ImageOperation.STRIP_METADATA,
    }
)


class ImageSuiteUI:
    """Renders the Image Suite panel and handles EventBus updates.

    Args:
        module_id: The module's dot-separated ID (used for logging).
    """

    def __init__(self, module_id: str) -> None:
        self._module_id = module_id
        self._is_running = False
        self._files: list[Path] = []

        stored = str(
            store_get(
                STORAGE_TABLE,
                STORAGE_KEY_LAST_OPERATION,
                default=ImageOperation.CONVERT.value,
            )
        )
        self._operation = _coerce_operation(stored)
        self._last_dir = str(store_get(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, default=""))

        self._output_format = OutputFormat.PNG
        self._quality = DEFAULT_QUALITY
        self._target_kb = DEFAULT_TARGET_KB
        self._max_dimension = DEFAULT_MAX_DIMENSION
        self._base_size_dp = 24

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
        """Build the Image Suite UI inside *container*.

        Args:
            container: NiceGUI parent element.
        """
        with ui.column().style("gap: 20px; width: 100%; max-width: 860px;"):
            module_header(
                "Image Suite",
                "Convert, compress and sanitise images; export SVG assets.",
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
        """Render the operation picker and its options."""
        with section_card():
            section_title("Operation")
            ui.select(
                options=OPERATION_LABELS,
                value=self._operation.value,
                on_change=self._on_operation_change,
            ).props("outlined dense options-dense").style("width: 100%;")

            self._options = ui.column().style(
                "gap: 10px; width: 100%; margin-top: 14px;"
            )
            self._render_options()

    def _render_options(self) -> None:
        """Repaint the options that apply to the selected operation."""
        if self._options is None:
            return
        self._options.clear()
        with self._options:
            if self._operation in _FORMAT_OPERATIONS:
                ui.select(
                    options={f.value: f.value.upper() for f in OutputFormat},
                    value=self._output_format.value,
                    label="Output format",
                    on_change=lambda e: setattr(
                        self, "_output_format", OutputFormat(str(e.value))
                    ),
                ).props("outlined dense").style("width: 100%;")

            if self._operation is ImageOperation.COMPRESS_TO_TARGET:
                ui.number(
                    label="Target size (KB)",
                    value=self._target_kb,
                    min=1,
                    precision=0,
                    on_change=lambda e: setattr(
                        self, "_target_kb", int(e.value or DEFAULT_TARGET_KB)
                    ),
                ).props("outlined dense").style("width: 100%;")
                ui.label(
                    "Encoder quality is searched automatically to land just "
                    "under this size. PNG is lossless, so choose JPEG or WebP."
                ).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
            elif self._operation is ImageOperation.RESIZE:
                ui.number(
                    label="Longest edge (px)",
                    value=self._max_dimension,
                    min=16,
                    precision=0,
                    on_change=lambda e: setattr(
                        self, "_max_dimension", int(e.value or DEFAULT_MAX_DIMENSION)
                    ),
                ).props("outlined dense").style("width: 100%;")
                ui.label(
                    "Smaller images are left alone rather than upscaled."
                ).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
            elif self._operation is ImageOperation.SVG_TO_DENSITIES:
                ui.number(
                    label="Base size (dp)",
                    value=self._base_size_dp,
                    min=1,
                    precision=0,
                    on_change=lambda e: setattr(
                        self, "_base_size_dp", int(e.value or 24)
                    ),
                ).props("outlined dense").style("width: 100%;")
                ui.label(
                    "Exports mdpi through xxxhdpi at 1x to 4x this size."
                ).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")

            if self._operation in _FORMAT_OPERATIONS and (
                self._operation is not ImageOperation.COMPRESS_TO_TARGET
            ):
                ui.slider(
                    min=MIN_QUALITY,
                    max=MAX_QUALITY,
                    value=self._quality,
                    on_change=lambda e: setattr(self, "_quality", int(e.value)),
                ).props("label-always").style("width: 100%;")
                ui.label("Encoder quality (ignored for PNG, which is lossless).").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 11px;"
                )

    def _render_files_card(self) -> None:
        """Render the input file list and its controls."""
        with section_card():
            with ui.row().style(
                "align-items: center; justify-content: space-between; width: 100%;"
            ):
                section_title("Input Images")
                with ui.row().style("gap: 8px;"):
                    ui.button("Add Images…", on_click=self._on_add_files).props(
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

            with ui.row().style(
                "justify-content: flex-end; width: 100%; margin-top: 14px;"
            ):
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
                ui.label("No images selected yet.").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 10px 2px;"
                )
                return
            for index, path in enumerate(self._files):
                self._render_file_row(index, path)

    def _render_file_row(self, index: int, path: Path) -> None:
        """Render one file row with its remove control.

        Args:
            index: Position in the list.
            path: The file.
        """
        with ui.row().style(
            f"align-items: center; gap: 8px; width: 100%; padding: 6px 10px;"
            f" background: {COLOR_OVERLAY}; border-radius: 6px;"
        ):
            ui.label(path.name).style(
                f"color: {COLOR_TEXT_MUTED}; font-size: 12px; flex: 1;"
                " font-family: monospace; word-break: break-all;"
            )
            ui.label(format_bytes(path.stat().st_size if path.is_file() else 0)).style(
                f"color: {COLOR_TEXT_DIM}; font-size: 11px;"
            )
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
        """Pick input images, filtered to what the operation accepts."""
        extensions = INPUT_EXTENSIONS[self._operation]
        start = Path(self._last_dir) if self._last_dir else None
        chosen = await choose_files(start, extensions=extensions, label="Images")
        if not chosen:
            return

        self._last_dir = str(chosen[0].parent)
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, self._last_dir)

        existing = {p.resolve() for p in self._files}
        rejected = 0
        for path in chosen:
            if path.resolve() in existing:
                continue
            if path.suffix.lower() not in extensions:
                rejected += 1
                continue
            self._files.append(path)
            existing.add(path.resolve())

        if rejected:
            ui.notify(
                f"Skipped {rejected} file(s) — this operation accepts "
                f"{', '.join(extensions)}.",
                type="warning",
            )
        self._refresh_file_list()

    def _on_clear_files(self) -> None:
        """Empty the file list."""
        self._files.clear()
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
        """Switch operation, dropping files it cannot read."""
        self._operation = _coerce_operation(str(event.value))
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_OPERATION, self._operation.value)

        accepted = INPUT_EXTENSIONS[self._operation]
        kept = [p for p in self._files if p.suffix.lower() in accepted]
        if len(kept) != len(self._files):
            ui.notify(
                f"Removed {len(self._files) - len(kept)} file(s) that this "
                "operation cannot read.",
                type="info",
            )
        self._files = kept

        self._render_options()
        self._refresh_file_list()

    async def _on_run_click(self) -> None:
        """Validate the form and publish the execute request."""
        if self._is_running:
            return
        if not self._files:
            ui.notify("Add at least one image.", type="warning")
            return

        try:
            params = ImageParams(
                operation=self._operation,
                input_paths=list(self._files),
                output_dir=self._output.path,
                output_format=self._output_format,
                quality=self._quality,
                target_kb=self._target_kb,
                max_dimension=self._max_dimension,
                base_size_dp=self._base_size_dp,
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
            payload: An ImageResult.
        """
        if not isinstance(payload, ImageResult):
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
        logger.error("image_suite.ui.error — %s", message)

    def _reset_controls(self) -> None:
        """Return the run button to its idle state."""
        self._is_running = False
        if self._run_btn is not None:
            self._run_btn.set_text("Run")

    def _render_result(self, result: ImageResult) -> None:
        """Populate the result card.

        Args:
            result: The completed operation's result.
        """
        ui.label(result.detail).style(
            f"color: {COLOR_POSITIVE}; font-size: 15px; font-weight: 700;"
            " margin-bottom: 12px;"
        )

        with ui.grid(columns=3).style("gap: 16px; width: 100%;"):
            stat_chip("🖼 Images", str(result.images_processed))
            stat_chip("📦 Output", format_bytes(result.output_bytes))
            saved = result.bytes_saved
            stat_chip(
                "📉 Saved" if saved >= 0 else "📈 Larger",
                f"{format_bytes(abs(saved))} ({abs(result.size_change_percent):.0f}%)",
                colour=COLOR_POSITIVE if saved > 0 else COLOR_WARNING,
            )

        for note in result.warnings:
            ui.label(f"⚠ {note}").style(
                f"color: {COLOR_WARNING}; font-size: 11px; margin-top: 6px;"
            )

        for path in result.output_paths:
            if path.is_dir():
                self._render_folder_row(path)
            else:
                output_file_actions(path)

    def _render_folder_row(self, path: Path) -> None:
        """Render a row for an output directory.

        Args:
            path: The produced directory.
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
            count = sum(1 for entry in path.rglob("*") if entry.is_file())
            ui.label(f"{count} files").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 11px;"
            )


def _coerce_operation(value: str) -> ImageOperation:
    """Convert a stored string to an operation, tolerating stale values.

    Args:
        value: The persisted operation name.

    Returns:
        The matching operation, or CONVERT when unrecognised.
    """
    try:
        return ImageOperation(value)
    except ValueError:
        return ImageOperation.CONVERT


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
