"""File Filter — NiceGUI presentation layer.

Renders the source picker, the scanned extension checklist and the output
mode, then publishes on the EventBus. It never imports logic.py (rule A-03).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import ui

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.storage import store_get, store_set
from modules.extractors.file_filter.constants import (
    DEFAULT_EXCLUDE_PATTERNS,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    EVENT_SCAN,
    EVENT_SCANNED,
    EVENT_UNDO,
    EXTENSION_LIST_HEIGHT_PX,
    EXTENSIONS_PER_PAGE,
    STORAGE_KEY_LAST_DIR,
    STORAGE_KEY_LAST_EXCLUDES,
    STORAGE_KEY_OUTPUT_DIR,
    STORAGE_TABLE,
)
from modules.extractors.file_filter.models import (
    FilterParams,
    FilterResult,
    MovedPair,
    OutputMode,
    ScanParams,
    ScanResult,
    UndoParams,
)
from shared.constants import (
    COLOR_BORDER,
    COLOR_NEGATIVE,
    COLOR_OVERLAY,
    COLOR_POSITIVE,
    COLOR_PRIMARY,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_WARNING,
)
from shared.formatters import format_bytes, format_impact
from shared.ui_components import (
    ErrorPanel,
    OutputDirectoryPicker,
    ProgressPanel,
    choose_directory,
    confirm_destructive,
    module_header,
    output_file_actions,
    pagination_controls,
    section_card,
    section_title,
    stat_chip,
)

logger = get_logger(__name__)

MODE_LABELS: dict[str, str] = {
    OutputMode.COPY.value: "Copy — leave the originals untouched",
    OutputMode.MOVE.value: "Move — take the files out of the source tree",
    OutputMode.ZIP.value: "Zip — pack everything into one archive",
    OutputMode.MANIFEST.value: "Manifest — just list what matched",
}

_HELP_TEXT = (
    "Point at a folder and press Scan. You will get every file type it "
    "contains with counts and sizes; tick the ones you want and choose what "
    "to do with them. Move is the only mode that changes the source: it "
    "relocates the files rather than copying them, and the run can be reversed "
    "with Undo."
)


class FileFilterUI:
    """Renders the File Filter panel and handles EventBus updates.

    Args:
        module_id: The module's dot-separated ID (used for logging).
    """

    def __init__(self, module_id: str) -> None:
        self._module_id = module_id
        self._is_running = False

        self._source_dir = str(
            store_get(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, default="")
        )
        self._excludes = "\n".join(
            store_get(
                STORAGE_TABLE,
                STORAGE_KEY_LAST_EXCLUDES,
                default=list(DEFAULT_EXCLUDE_PATTERNS),
            )
        )
        self._output_mode = OutputMode.COPY
        self._preserve_hierarchy = True
        self._scan: ScanResult | None = None
        self._selected: set[str] = set()
        self._page = 0

        self._dir_input: ui.input | None = None
        self._extension_list: ui.column | None = None
        self._summary_label: ui.label | None = None
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
        event_bus.subscribe(EVENT_SCANNED, self._on_scanned)
        event_bus.subscribe(EVENT_PROGRESS, self._on_progress)
        event_bus.subscribe(EVENT_DONE, self._on_done)
        event_bus.subscribe(EVENT_ERROR, self._on_error)
        event_bus.subscribe(EVENT_CANCELLED, self._on_cancelled)

    def unsubscribe(self) -> None:
        """Deregister EventBus handlers.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_SCANNED, self._on_scanned)
        event_bus.unsubscribe(EVENT_PROGRESS, self._on_progress)
        event_bus.unsubscribe(EVENT_DONE, self._on_done)
        event_bus.unsubscribe(EVENT_ERROR, self._on_error)
        event_bus.unsubscribe(EVENT_CANCELLED, self._on_cancelled)

    # ─── Render ───────────────────────────────────────────────────────────────

    def render(self, container: Any) -> None:
        """Build the File Filter UI inside *container*.

        Args:
            container: NiceGUI parent element.
        """
        with ui.column().style("gap: 20px; width: 100%; max-width: 860px;"):
            module_header(
                "File Filter",
                "Find what file types a tree holds, then extract the ones you want.",
                _HELP_TEXT,
            )
            self._render_source_card()
            self._render_extensions_card()
            self._render_output_card()
            with section_card():
                section_title("Destination")
                self._output.render()
            self._progress.render(on_cancel=self._on_cancel_click)
            self._errors.render()
            self._render_result_card()

    def _render_source_card(self) -> None:
        """Render the source directory picker and exclude patterns."""
        with section_card():
            section_title("Source")
            with ui.row().style("align-items: center; gap: 8px; width: 100%;"):
                self._dir_input = (
                    ui.input(
                        label="Folder to scan",
                        value=self._source_dir,
                        on_change=lambda e: setattr(self, "_source_dir", e.value or ""),
                    )
                    .props("outlined dense")
                    .style("flex: 1;")
                )
                ui.button("Browse", on_click=self._on_browse).props(
                    "flat dense no-caps"
                ).style(
                    f"color: {COLOR_TEXT_MUTED}; border: 1px solid {COLOR_BORDER};"
                    " border-radius: 6px;"
                )
                ui.button("Scan", on_click=self._on_scan_click).props("no-caps").style(
                    f"background: {COLOR_PRIMARY}; color: white; font-weight: 600;"
                    " border-radius: 6px;"
                )

            with ui.column().style("gap: 4px; width: 100%; margin-top: 12px;"):
                ui.label("Exclude patterns (one per line, gitignore style)").style(
                    f"color: {COLOR_TEXT_MUTED}; font-size: 12px;"
                )
                ui.textarea(
                    value=self._excludes,
                    on_change=lambda e: setattr(self, "_excludes", e.value or ""),
                ).props("outlined dense").style(
                    "width: 100%; font-family: monospace; font-size: 12px; height: 90px;"
                )

    def _render_extensions_card(self) -> None:
        """Render the scanned extension checklist."""
        with section_card():
            with ui.row().style(
                "align-items: center; justify-content: space-between; width: 100%;"
            ):
                section_title("File Types")
                with ui.row().style("gap: 8px;"):
                    ui.button("Select all", on_click=lambda: self._set_all(True)).props(
                        "flat dense no-caps"
                    ).style(f"color: {COLOR_TEXT_MUTED};")
                    ui.button("Clear", on_click=lambda: self._set_all(False)).props(
                        "flat dense no-caps"
                    ).style(f"color: {COLOR_TEXT_MUTED};")

            self._extension_list = ui.column().style(
                f"gap: 2px; width: 100%; max-height: {EXTENSION_LIST_HEIGHT_PX}px;"
                " overflow-y: auto;"
            )
            self._refresh_extensions()

            self._summary_label = ui.label("").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 12px; margin-top: 10px;"
            )
            self._refresh_summary()

    def _refresh_extensions(self) -> None:
        """Repaint the extension checklist."""
        if self._extension_list is None:
            return
        self._extension_list.clear()
        with self._extension_list:
            if self._scan is None:
                ui.label("Scan a folder to see what it contains.").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 10px 2px;"
                )
                return
            if not self._scan.extensions:
                ui.label("No files matched the exclude patterns.").style(
                    f"color: {COLOR_WARNING}; font-size: 12px; padding: 10px 2px;"
                )
                return

            # Paged rather than capped. A hard cap left the rarest types
            # unreachable — "Select all" covered them, but a user could not
            # tick one specific extension past the cut. Paging keeps the
            # rendered DOM bounded (rule E-05) while leaving every type
            # reachable. The list is already sorted by the logic (commonest
            # first), so the page order is meaningful.
            extensions = self._scan.extensions
            start = self._page * EXTENSIONS_PER_PAGE
            for entry in extensions[start : start + EXTENSIONS_PER_PAGE]:
                self._render_extension_row(
                    entry.extension, entry.count, entry.total_bytes
                )

            pagination_controls(
                self._page,
                len(extensions),
                EXTENSIONS_PER_PAGE,
                self._on_page,
                noun="file types",
            )

    def _render_extension_row(self, extension: str, count: int, size: int) -> None:
        """Render one extension row with its checkbox and counts.

        Args:
            extension: The extension label.
            count: Number of files.
            size: Combined size in bytes.
        """
        with ui.row().style(
            "align-items: center; gap: 10px; width: 100%; padding: 4px 8px;"
        ):
            ui.checkbox(
                value=extension in self._selected,
                on_change=lambda e, ext=extension: self._toggle(ext, bool(e.value)),
            ).props("dense")
            ui.label(extension).style(
                f"color: {COLOR_TEXT_MUTED}; font-size: 13px;"
                " font-family: monospace; min-width: 120px;"
            )
            ui.label(f"{count:,} files").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 12px; flex: 1;"
            )
            ui.label(format_bytes(size)).style(
                f"color: {COLOR_TEXT_DIM}; font-size: 12px;"
            )

    def _render_output_card(self) -> None:
        """Render the output mode picker and the run button."""
        with section_card():
            section_title("Output")
            ui.select(
                options=MODE_LABELS,
                value=self._output_mode.value,
                on_change=self._on_mode_change,
            ).props("outlined dense options-dense").style("width: 100%;")

            ui.checkbox(
                "Preserve folder structure",
                value=self._preserve_hierarchy,
                on_change=lambda e: setattr(
                    self, "_preserve_hierarchy", bool(e.value)
                ),
            ).props("dense").style("margin-top: 10px;")

            with ui.row().style(
                "justify-content: flex-end; width: 100%; margin-top: 14px;"
            ):
                self._run_btn = ui.button("Run", on_click=self._on_run_click).props(
                    "no-caps"
                ).style(
                    f"background: {COLOR_PRIMARY}; color: white; font-weight: 600;"
                    " padding: 8px 24px; border-radius: 6px;"
                )

    def _render_result_card(self) -> None:
        """Render the (initially empty) result card."""
        with section_card() as self._result_card:
            ui.label("Results will appear here.").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 13px;"
            )

    # ─── Selection ────────────────────────────────────────────────────────────

    def _toggle(self, extension: str, checked: bool) -> None:
        """Add or remove an extension from the selection.

        Args:
            extension: The extension label.
            checked: Its new checked state.
        """
        if checked:
            self._selected.add(extension)
        else:
            self._selected.discard(extension)
        self._refresh_summary()

    def _on_page(self, page: int) -> None:
        """Move the extension list to *page* and repaint.

        Args:
            page: The 0-based page to show.
        """
        self._page = page
        self._refresh_extensions()

    def _set_all(self, checked: bool) -> None:
        """Select or clear every scanned extension.

        Args:
            checked: True to select all, False to clear.
        """
        if self._scan is None:
            return
        self._selected = (
            {e.extension for e in self._scan.extensions} if checked else set()
        )
        self._refresh_extensions()
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        """Update the line describing the current selection."""
        if self._summary_label is None:
            return
        if self._scan is None:
            self._summary_label.set_text("")
            return

        chosen = [e for e in self._scan.extensions if e.extension in self._selected]
        if not chosen:
            self._summary_label.set_text(
                f"{self._scan.total_files:,} files found · nothing selected"
            )
            return
        files = sum(e.count for e in chosen)
        size = sum(e.total_bytes for e in chosen)
        self._summary_label.set_text(f"Selected: {format_impact(size, files)}")

    # ─── Controls ─────────────────────────────────────────────────────────────

    async def _on_browse(self) -> None:
        """Pick the source folder."""
        start = Path(self._source_dir) if self._source_dir.strip() else None
        chosen = await choose_directory(start)
        if chosen is None:
            return
        self._source_dir = str(chosen)
        if self._dir_input is not None:
            self._dir_input.set_value(self._source_dir)

    async def _on_scan_click(self) -> None:
        """Validate the source and request a scan."""
        source = self._source_dir.strip()
        if not source:
            ui.notify("Choose a folder to scan.", type="warning")
            return

        try:
            params = ScanParams(
                source_dir=Path(source), exclude_patterns=self._exclude_list()
            )
        except ValueError as exc:
            ui.notify(_first_error(exc), type="negative")
            return

        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, source)
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_EXCLUDES, self._exclude_list())

        self._progress.update(0, "Scanning…")
        await event_bus.publish(EVENT_SCAN, params)

    def _on_mode_change(self, event: Any) -> None:
        """Switch output mode."""
        self._output_mode = OutputMode(str(event.value))

    async def _on_run_click(self) -> None:
        """Validate the form and start the filter, confirming destructive runs."""
        if self._is_running:
            return
        if self._scan is None:
            ui.notify("Scan a folder first.", type="warning")
            return
        if not self._selected:
            ui.notify("Tick at least one file type.", type="warning")
            return

        try:
            params = FilterParams(
                source_dir=Path(self._source_dir.strip()),
                extensions=sorted(self._selected),
                exclude_patterns=self._exclude_list(),
                output_mode=self._output_mode,
                output_dir=self._output.path,
                preserve_hierarchy=self._preserve_hierarchy,
            )
        except ValueError as exc:
            ui.notify(_first_error(exc), type="negative")
            return

        if params.is_destructive:
            chosen = [
                e for e in self._scan.extensions if e.extension in self._selected
            ]
            await confirm_destructive(
                title="Move these files out of the source folder?",
                impact=format_impact(
                    sum(e.total_bytes for e in chosen), sum(e.count for e in chosen)
                ),
                detail=(
                    "The originals are copied first, then moved to the recycle "
                    "store, where they can be restored for 24 hours."
                ),
                confirm_label="Move Files",
                on_confirm=lambda: self._start(params),
            )
            return

        await self._start(params)

    async def _start(self, params: FilterParams) -> None:
        """Publish the execute request and lock the run button.

        Args:
            params: The validated filter parameters.
        """
        self._is_running = True
        self._errors.hide()
        self._progress.update(0, "Starting…")
        if self._run_btn is not None:
            self._run_btn.set_text("Working…")
        await event_bus.publish(EVENT_EXECUTE, params)

    async def _on_undo_click(self, pairs: list[MovedPair]) -> None:
        """Ask the logic layer to move a completed Move run back.

        Args:
            pairs: The relocations to reverse.
        """
        if self._is_running:
            return
        self._is_running = True
        self._errors.hide()
        self._progress.update(0, "Moving files back…")
        await event_bus.publish(EVENT_UNDO, UndoParams(pairs=pairs))

    def _exclude_list(self) -> list[str]:
        """Return the exclude patterns as a cleaned list."""
        return [line.strip() for line in self._excludes.splitlines() if line.strip()]

    # ─── EventBus handlers ────────────────────────────────────────────────────

    async def _on_scanned(self, payload: Any) -> None:
        """Populate the extension checklist from a completed scan.

        Args:
            payload: A ScanResult.
        """
        if not isinstance(payload, ScanResult):
            return
        self._scan = payload
        self._selected = set()
        self._page = 0  # a new scan starts at the first page
        self._refresh_extensions()
        self._refresh_summary()
        self._progress.reset(
            f"Found {payload.total_files:,} files across "
            f"{len(payload.extensions)} file types."
        )

    async def _on_progress(self, payload: Any) -> None:
        """Advance the progress panel.

        Args:
            payload: A ProgressEvent.
        """
        if isinstance(payload, ProgressEvent):
            self._progress.update(payload.percent, payload.message)

    async def _on_done(self, payload: Any) -> None:
        """Render the result card once a filter completes.

        Args:
            payload: A FilterResult.
        """
        if not isinstance(payload, FilterResult):
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
        self._errors.show("The filter could not be completed.", message)
        logger.error("file_filter.ui.error — %s", message)

    def _reset_controls(self) -> None:
        """Return the run button to its idle state."""
        self._is_running = False
        if self._run_btn is not None:
            self._run_btn.set_text("Run")

    def _render_result(self, result: FilterResult) -> None:
        """Populate the result card.

        Args:
            result: The completed run's result.
        """
        ui.label(result.detail).style(
            f"color: {COLOR_POSITIVE}; font-size: 15px; font-weight: 700;"
            " margin-bottom: 12px;"
        )

        with ui.grid(columns=3).style("gap: 16px; width: 100%;"):
            stat_chip("🔍 Matched", f"{result.files_matched:,}")
            stat_chip("✅ Written", f"{result.files_written:,}")
            stat_chip("📦 Size", format_bytes(result.total_bytes))

        if result.moved_pairs:
            moved = list(result.moved_pairs)
            with ui.row().style(
                f"align-items: center; gap: 8px; margin-top: 12px; padding: 10px 14px;"
                f" background: {COLOR_OVERLAY}; border-radius: 6px; width: 100%;"
            ):
                ui.icon("undo").style(f"color: {COLOR_WARNING}; font-size: 18px;")
                ui.label(
                    f"{len(moved)} file(s) were relocated out of the source tree."
                ).style(f"color: {COLOR_TEXT_MUTED}; font-size: 11px; flex: 1;")
                ui.button(
                    "Undo Move",
                    on_click=lambda: self._on_undo_click(moved),
                ).props("flat dense no-caps").style(f"color: {COLOR_WARNING};")

        for note in result.warnings:
            ui.label(f"⚠ {note}").style(
                f"color: {COLOR_NEGATIVE}; font-size: 11px; margin-top: 6px;"
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
            ui.label(f"{count:,} files").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 11px;"
            )


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
