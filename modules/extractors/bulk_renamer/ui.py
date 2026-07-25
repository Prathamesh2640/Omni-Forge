"""Bulk Regex Renamer — NiceGUI presentation layer.

Renders the source picker, the pattern/replacement inputs, a live preview
table, and the run/undo controls, then publishes on the EventBus. It never
imports logic.py (rule A-03).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import ui

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.storage import store_get, store_set
from modules.extractors.bulk_renamer.constants import (
    DEFAULT_EXCLUDE_PATTERNS,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PREVIEW,
    EVENT_PREVIEWED,
    EVENT_PROGRESS,
    MAX_PREVIEW_ROWS,
    PREVIEW_TABLE_HEADER_HEIGHT_PX,
    PREVIEW_TABLE_HEIGHT_PX,
    PREVIEW_TABLE_ROW_HEIGHT_PX,
    STORAGE_KEY_LAST_DIR,
    STORAGE_KEY_LAST_EXCLUDES,
    STORAGE_TABLE,
)
from modules.extractors.bulk_renamer.models import (
    PreviewResult,
    RenameParams,
    RenamePreviewEntry,
    RenameResult,
    UndoParams,
)
from shared.constants import (
    COLOR_BORDER,
    COLOR_DARK_BG,
    COLOR_NEGATIVE,
    COLOR_POSITIVE,
    COLOR_PRIMARY,
    COLOR_PRIMARY_SOFT,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_WARNING,
)
from shared.ui_components import (
    ErrorPanel,
    ProgressPanel,
    choose_directory,
    module_header,
    section_card,
    section_title,
    stat_chip,
)

logger = get_logger(__name__)

_HELP_TEXT = (
    "Point at a folder, write a regular expression and a replacement, then "
    "press Preview to see exactly what would change before anything does. "
    "The pattern matches each file's name without its extension, so the "
    "extension is always kept. The replacement can use regex backreferences "
    "(\\1), a per-match counter ({n} or {n:03d}), and today's date ({date}). "
    "A rename that would collide with another file's new name — or with a "
    "different existing file — is skipped rather than overwriting anything. "
    "Renaming is reversible: Undo appears right after a run completes."
)


class BulkRenamerUI:
    """Renders the Bulk Regex Renamer panel and handles EventBus updates.

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
        self._recursive = False
        self._pattern = ""
        self._replacement = ""
        self._preview: PreviewResult | None = None

        self._dir_input: ui.input | None = None
        self._preview_table: ui.aggrid | None = None
        self._summary_label: ui.label | None = None
        self._run_btn: ui.button | None = None
        self._result_card: ui.card | None = None
        self._progress = ProgressPanel()
        self._errors = ErrorPanel()

    def subscribe(self) -> None:
        """Register EventBus handlers.  Call once from ``on_load()``."""
        event_bus.subscribe(EVENT_PREVIEWED, self._on_previewed)
        event_bus.subscribe(EVENT_PROGRESS, self._on_progress)
        event_bus.subscribe(EVENT_DONE, self._on_done)
        event_bus.subscribe(EVENT_ERROR, self._on_error)
        event_bus.subscribe(EVENT_CANCELLED, self._on_cancelled)

    def unsubscribe(self) -> None:
        """Deregister EventBus handlers.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_PREVIEWED, self._on_previewed)
        event_bus.unsubscribe(EVENT_PROGRESS, self._on_progress)
        event_bus.unsubscribe(EVENT_DONE, self._on_done)
        event_bus.unsubscribe(EVENT_ERROR, self._on_error)
        event_bus.unsubscribe(EVENT_CANCELLED, self._on_cancelled)

    # ─── Render ───────────────────────────────────────────────────────────────

    def render(self, container: Any) -> None:
        """Build the Bulk Regex Renamer UI inside *container*.

        Args:
            container: NiceGUI parent element.
        """
        with ui.column().style("gap: 20px; width: 100%; max-width: 900px;"):
            module_header(
                "Bulk Regex Renamer",
                "Rename many files at once with a regular expression — preview first.",
                _HELP_TEXT,
            )
            self._render_source_card()
            self._render_pattern_card()
            self._render_preview_card()
            self._progress.render(on_cancel=self._on_cancel_click)
            self._errors.render()
            self._render_result_card()

    def _render_source_card(self) -> None:
        """Render the source directory picker, recursion toggle and excludes."""
        with section_card():
            section_title("Source")
            with ui.row().style("align-items: center; gap: 8px; width: 100%;"):
                self._dir_input = (
                    ui.input(
                        label="Folder",
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

            ui.checkbox(
                "Include subfolders",
                value=self._recursive,
                on_change=lambda e: setattr(self, "_recursive", bool(e.value)),
            ).props("dense").style("margin-top: 8px;")

            with ui.column().style("gap: 4px; width: 100%; margin-top: 8px;"):
                ui.label(
                    "Exclude patterns (one per line, gitignore style — only "
                    "applies with subfolders included)"
                ).style(f"color: {COLOR_TEXT_MUTED}; font-size: 12px;")
                ui.textarea(
                    value=self._excludes,
                    on_change=lambda e: setattr(self, "_excludes", e.value or ""),
                ).props("outlined dense").style(
                    "width: 100%; font-family: monospace; font-size: 12px; height: 70px;"
                )

    def _render_pattern_card(self) -> None:
        """Render the pattern/replacement inputs and the Preview button."""
        with section_card():
            section_title("Pattern")
            ui.input(
                label="Match (regular expression, applied to the file name without its extension)",
                value=self._pattern,
                on_change=lambda e: setattr(self, "_pattern", e.value or ""),
            ).props("outlined dense").style("width: 100%; font-family: monospace;")
            ui.input(
                label="Replace with (\\1 backreferences, {n} counter, {date})",
                value=self._replacement,
                on_change=lambda e: setattr(self, "_replacement", e.value or ""),
            ).props("outlined dense").style(
                "width: 100%; font-family: monospace; margin-top: 10px;"
            )

            with ui.row().style("justify-content: flex-end; width: 100%; margin-top: 12px;"):
                ui.button("Preview", on_click=self._on_preview_click).props("no-caps").style(
                    f"background: {COLOR_PRIMARY}; color: white; font-weight: 600;"
                    " border-radius: 6px;"
                )

    def _render_preview_card(self) -> None:
        """Render the live preview table and the run button."""
        with section_card():
            with ui.row().style(
                "align-items: baseline; justify-content: space-between; width: 100%;"
            ):
                section_title("Preview")
                self._summary_label = ui.label("Press Preview to see what would change.").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 12px;"
                )

            self._preview_table = ui.aggrid(
                {
                    "columnDefs": [
                        {"headerName": "Original", "field": "original", "flex": 2},
                        {"headerName": "Proposed", "field": "proposed", "flex": 2},
                        {"headerName": "Status", "field": "status", "width": 140},
                    ],
                    "rowData": [],
                    "suppressCellFocus": True,
                    "rowHeight": PREVIEW_TABLE_ROW_HEIGHT_PX,
                    "headerHeight": PREVIEW_TABLE_HEADER_HEIGHT_PX,
                }
            ).style(
                f"width: 100%; height: {PREVIEW_TABLE_HEIGHT_PX}px; margin-top: 10px;"
                " --ag-background-color: transparent;"
                f" --ag-header-background-color: {COLOR_DARK_BG};"
                f" --ag-row-hover-color: {COLOR_PRIMARY_SOFT};"
                f" --ag-foreground-color: {COLOR_TEXT_MUTED};"
                f" --ag-header-foreground-color: {COLOR_TEXT_DIM};"
                f" --ag-border-color: {COLOR_BORDER};"
                " --ag-odd-row-background-color: transparent;"
            )

            with ui.row().style("justify-content: flex-end; width: 100%; margin-top: 14px;"):
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

    # ─── Preview table ────────────────────────────────────────────────────────

    def _status_label(self, entry: RenamePreviewEntry) -> str:
        """Return the short status text shown in the preview table's row.

        Args:
            entry: The preview entry to describe.
        """
        if entry.conflict:
            return "⚠ Conflict"
        if entry.unsafe:
            return "⚠ Unsafe"
        if not entry.matched:
            return "— unmatched"
        if entry.proposed_name == entry.original_name:
            return "No change"
        return "✓ Rename"

    async def _refresh_preview_table(self) -> None:
        """Repaint the ag-grid rows from the current preview.

        Mutating ``.options`` alone does not reach an already-rendered
        ag-grid instance — the client-side grid keeps its own state, so the
        update must go through ``run_grid_method`` (matching live_monitor's
        process table).
        """
        if self._preview_table is None:
            return
        rows = (
            [
                {
                    "original": entry.original_name,
                    "proposed": entry.proposed_name,
                    "status": self._status_label(entry),
                }
                for entry in self._preview.entries[:MAX_PREVIEW_ROWS]
            ]
            if self._preview is not None
            else []
        )
        self._preview_table.options["rowData"] = rows
        await self._preview_table.run_grid_method("setGridOption", "rowData", rows)

    def _refresh_summary(self) -> None:
        """Update the preview summary line."""
        if self._summary_label is None:
            return
        if self._preview is None:
            self._summary_label.set_text("Press Preview to see what would change.")
            return
        hidden = self._preview.total_files - min(self._preview.total_files, MAX_PREVIEW_ROWS)
        hidden_note = f" ({hidden} more not shown)" if hidden > 0 else ""
        self._summary_label.set_text(
            f"{self._preview.total_files:,} files · {self._preview.matched_count} matched · "
            f"{self._preview.renameable_count} would rename{hidden_note}"
        )

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

    def _build_params(self, plan: list[RenamePreviewEntry] | None = None) -> RenameParams | None:
        """Validate the form and build RenameParams, notifying on failure.

        Args:
            plan: The preview the user approved, when this is a run rather
                than a preview request. Sending it makes the logic apply
                exactly what was on screen instead of recomputing it.

        Returns:
            The validated parameters, or None when the form is invalid.
        """
        source = self._source_dir.strip()
        if not source:
            ui.notify("Choose a folder.", type="warning")
            return None
        if not self._pattern.strip():
            ui.notify("Enter a pattern to match.", type="warning")
            return None

        try:
            return RenameParams(
                source_dir=Path(source),
                pattern=self._pattern,
                replacement=self._replacement,
                recursive=self._recursive,
                exclude_patterns=self._exclude_list(),
                plan=plan or [],
            )
        except ValueError as exc:
            ui.notify(_first_error(exc), type="negative")
            return None

    async def _on_preview_click(self) -> None:
        """Validate the form and request a preview."""
        params = self._build_params()
        if params is None:
            return

        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, self._source_dir.strip())
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_EXCLUDES, self._exclude_list())

        self._progress.update(0, "Computing preview…")
        await event_bus.publish(EVENT_PREVIEW, params)

    async def _on_run_click(self) -> None:
        """Start the rename, requiring a preview first."""
        if self._is_running:
            return
        if self._preview is None:
            ui.notify("Preview first.", type="warning")
            return
        if self._preview.renameable_count == 0:
            ui.notify("Nothing would change.", type="warning")
            return

        # Send the approved preview along, so the run applies exactly what is
        # on screen rather than recomputing it against a moved-on filesystem.
        params = self._build_params(plan=self._preview.entries)
        if params is None:
            return
        await self._start(params)

    async def _on_undo_click(self, result: RenameResult) -> None:
        """Reverse the given result's renames.

        Args:
            result: The RenameResult (rename or a previous undo) to reverse.
        """
        if self._is_running:
            return
        await self._start(UndoParams(pairs=result.renamed))

    async def _start(self, params: RenameParams | UndoParams) -> None:
        """Publish the execute request and lock the run button.

        Args:
            params: The validated rename or undo parameters.
        """
        self._is_running = True
        self._errors.hide()
        self._progress.update(0, "Starting…")
        if self._run_btn is not None:
            self._run_btn.set_text("Working…")
        await event_bus.publish(EVENT_EXECUTE, params)

    def _exclude_list(self) -> list[str]:
        """Return the exclude patterns as a cleaned list."""
        return [line.strip() for line in self._excludes.splitlines() if line.strip()]

    # ─── EventBus handlers ────────────────────────────────────────────────────

    async def _on_previewed(self, payload: Any) -> None:
        """Populate the preview table from a completed preview.

        Args:
            payload: A PreviewResult.
        """
        if not isinstance(payload, PreviewResult):
            return
        self._preview = payload
        await self._refresh_preview_table()
        self._refresh_summary()
        self._progress.reset(
            f"{payload.renameable_count} of {payload.total_files} files would be renamed."
        )

    async def _on_progress(self, payload: Any) -> None:
        """Advance the progress panel.

        Args:
            payload: A ProgressEvent.
        """
        if isinstance(payload, ProgressEvent):
            self._progress.update(payload.percent, payload.message)

    async def _on_done(self, payload: Any) -> None:
        """Render the result card once a rename or undo completes.

        Args:
            payload: A RenameResult.
        """
        if not isinstance(payload, RenameResult):
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
        logger.error("bulk_renamer.ui.error — %s", message)

    def _reset_controls(self) -> None:
        """Return the run button to its idle state."""
        self._is_running = False
        if self._run_btn is not None:
            self._run_btn.set_text("Run")

    def _render_result(self, result: RenameResult) -> None:
        """Populate the result card.

        Args:
            result: The completed run's result.
        """
        with ui.grid(columns=2).style("gap: 16px; width: 100%;"):
            stat_chip("✓ Renamed", f"{result.files_renamed:,}")
            stat_chip("⚠ Skipped", f"{result.skipped_count:,}")

        if result.renamed:
            with ui.row().style("justify-content: flex-end; width: 100%; margin-top: 14px;"):
                ui.button(
                    "Undo", on_click=lambda: self._on_undo_click(result)
                ).props("no-caps outline").style(
                    f"color: {COLOR_WARNING}; border: 1px solid {COLOR_WARNING};"
                    " border-radius: 6px; font-weight: 600;"
                )

        for note in result.warnings:
            ui.label(f"⚠ {note}").style(
                f"color: {COLOR_NEGATIVE}; font-size: 11px; margin-top: 6px;"
            )

        if not result.warnings:
            ui.label("No conflicts or errors.").style(
                f"color: {COLOR_POSITIVE}; font-size: 11px; margin-top: 6px;"
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
