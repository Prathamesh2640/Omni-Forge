"""Document Suite — NiceGUI presentation layer.

Renders the conversion picker, file list and options, then publishes
ConvertParams on the EventBus. It never imports logic.py (rule A-03).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import ui

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.storage import store_get, store_set
from modules.converters.document_suite.constants import (
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    FILE_LIST_HEIGHT_PX,
    STORAGE_KEY_LAST_CONVERSION,
    STORAGE_KEY_LAST_DIR,
    STORAGE_KEY_OUTPUT_DIR,
    STORAGE_TABLE,
)
from modules.converters.document_suite.models import (
    INPUT_EXTENSIONS,
    Conversion,
    ConvertParams,
    ConvertResult,
)
from shared.constants import (
    COLOR_OVERLAY,
    COLOR_POSITIVE,
    COLOR_PRIMARY,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
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

#: Conversion picker labels.
CONVERSION_LABELS: dict[str, str] = {
    Conversion.MARKDOWN_TO_HTML.value: "Markdown → HTML (self-contained page)",
    Conversion.HTML_TO_MARKDOWN.value: "HTML → Markdown",
    Conversion.JSON_TO_CSV.value: "JSON → CSV",
    Conversion.CSV_TO_JSON.value: "CSV / TSV → JSON",
    Conversion.JSON_TO_EXCEL.value: "JSON → Excel (.xlsx)",
    Conversion.JSON_TO_YAML.value: "JSON → YAML",
    Conversion.YAML_TO_JSON.value: "YAML → JSON",
}

_HELP_TEXT = (
    "Pick a conversion, add the files, then run it. Each file is converted "
    "independently and written to exports/document_suite. Everything runs on "
    "this machine — nothing is uploaded."
)

#: Conversions that produce tabular output and honour the flattening option.
_TABULAR_CONVERSIONS = frozenset(
    {Conversion.JSON_TO_CSV, Conversion.JSON_TO_EXCEL}
)


class DocumentSuiteUI:
    """Renders the Document Suite panel and handles EventBus updates.

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
                STORAGE_KEY_LAST_CONVERSION,
                default=Conversion.MARKDOWN_TO_HTML.value,
            )
        )
        self._conversion = _coerce_conversion(stored)
        self._last_dir = str(store_get(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, default=""))
        self._flatten_nested = True
        self._infer_types = True

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
        """Build the Document Suite UI inside *container*.

        Args:
            container: NiceGUI parent element.
        """
        with ui.column().style("gap: 20px; width: 100%; max-width: 860px;"):
            module_header(
                "Document Suite",
                "Convert between Markdown, HTML, JSON, YAML, CSV and Excel.",
                _HELP_TEXT,
            )
            self._render_conversion_card()
            self._render_files_card()
            with section_card():
                section_title("Destination")
                self._output.render()
            self._progress.render(on_cancel=self._on_cancel_click)
            self._errors.render()
            self._render_result_card()

    def _render_conversion_card(self) -> None:
        """Render the conversion picker and its options."""
        with section_card():
            section_title("Conversion")
            ui.select(
                options=CONVERSION_LABELS,
                value=self._conversion.value,
                on_change=self._on_conversion_change,
            ).props("outlined dense options-dense").style("width: 100%;")

            self._options = ui.column().style(
                "gap: 6px; width: 100%; margin-top: 14px;"
            )
            self._render_options()

    def _render_options(self) -> None:
        """Repaint the options that apply to the selected conversion."""
        if self._options is None:
            return
        self._options.clear()
        with self._options:
            if self._conversion in _TABULAR_CONVERSIONS:
                ui.checkbox(
                    "Flatten nested objects into dotted columns",
                    value=self._flatten_nested,
                    on_change=lambda e: setattr(self, "_flatten_nested", bool(e.value)),
                ).props("dense")
                ui.label(
                    'With this on, {"user": {"city": "Pune"}} becomes a '
                    '"user.city" column. With it off, nested values are kept '
                    "as JSON text in one cell."
                ).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
            elif self._conversion is Conversion.CSV_TO_JSON:
                ui.checkbox(
                    "Infer numbers, booleans and nulls from text",
                    value=self._infer_types,
                    on_change=lambda e: setattr(self, "_infer_types", bool(e.value)),
                ).props("dense")
                ui.label(
                    'With this off every value stays a string, so "007" is '
                    "preserved rather than becoming 7."
                ).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
            else:
                ui.label(
                    f"Accepts: {', '.join(INPUT_EXTENSIONS[self._conversion])}"
                ).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")

    def _render_files_card(self) -> None:
        """Render the input file list and its controls."""
        with section_card():
            with ui.row().style(
                "align-items: center; justify-content: space-between; width: 100%;"
            ):
                section_title("Input Files")
                with ui.row().style("gap: 8px;"):
                    ui.button("Add Files…", on_click=self._on_add_files).props(
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
                self._run_btn = ui.button("Convert", on_click=self._on_run_click).props(
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
        """Pick input files, filtered to what the conversion accepts."""
        extensions = INPUT_EXTENSIONS[self._conversion]
        start = Path(self._last_dir) if self._last_dir else None
        chosen = await choose_files(
            start, extensions=extensions, label=f"{self._conversion.name.title()} input"
        )
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
                f"Skipped {rejected} file(s) — this conversion accepts "
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

    def _on_conversion_change(self, event: Any) -> None:
        """Switch conversion, dropping files it cannot read."""
        self._conversion = _coerce_conversion(str(event.value))
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_CONVERSION, self._conversion.value)

        accepted = INPUT_EXTENSIONS[self._conversion]
        kept = [p for p in self._files if p.suffix.lower() in accepted]
        if len(kept) != len(self._files):
            ui.notify(
                f"Removed {len(self._files) - len(kept)} file(s) that this "
                "conversion cannot read.",
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
            ui.notify("Add at least one file.", type="warning")
            return

        try:
            params = ConvertParams(
                conversion=self._conversion,
                input_paths=list(self._files),
                output_dir=self._output.path,
                flatten_nested=self._flatten_nested,
                infer_types=self._infer_types,
            )
        except ValueError as exc:
            ui.notify(_first_error(exc), type="negative")
            return

        self._is_running = True
        self._errors.hide()
        self._progress.update(0, "Starting…")
        if self._run_btn is not None:
            self._run_btn.set_text("Converting…")

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
        """Render the result card once a conversion completes.

        Args:
            payload: A ConvertResult.
        """
        if not isinstance(payload, ConvertResult):
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
        self._errors.show("The conversion could not be completed.", message)
        logger.error("document_suite.ui.error — %s", message)

    def _reset_controls(self) -> None:
        """Return the run button to its idle state."""
        self._is_running = False
        if self._run_btn is not None:
            self._run_btn.set_text("Convert")

    def _render_result(self, result: ConvertResult) -> None:
        """Populate the result card.

        Args:
            result: The completed conversion's result.
        """
        ui.label(result.detail).style(
            f"color: {COLOR_POSITIVE}; font-size: 15px; font-weight: 700;"
            " margin-bottom: 12px;"
        )

        with ui.grid(columns=3).style("gap: 16px; width: 100%;"):
            stat_chip("📄 Files", str(len(result.output_paths)))
            stat_chip("🔢 Records", f"{result.records:,}")
            stat_chip("📦 Output", format_bytes(result.output_bytes))

        for path in result.output_paths:
            output_file_actions(path)


def _coerce_conversion(value: str) -> Conversion:
    """Convert a stored string to a conversion, tolerating stale values.

    Args:
        value: The persisted conversion name.

    Returns:
        The matching conversion, or MARKDOWN_TO_HTML when unrecognised.
    """
    try:
        return Conversion(value)
    except ValueError:
        return Conversion.MARKDOWN_TO_HTML


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
