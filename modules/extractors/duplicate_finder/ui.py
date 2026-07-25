"""Duplicate Detective — NiceGUI presentation layer.

Renders the source picker, the scanned duplicate groups and the keep
strategy, then publishes on the EventBus. It never imports logic.py
(rule A-03).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import ui

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.storage import store_get, store_set
from modules.extractors.duplicate_finder.constants import (
    DEFAULT_EXCLUDE_PATTERNS,
    DEFAULT_MIN_SIZE_BYTES,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    EVENT_SCAN,
    EVENT_SCANNED,
    GROUP_LIST_HEIGHT_PX,
    GROUPS_PER_PAGE,
    STORAGE_KEY_LAST_DIR,
    STORAGE_KEY_LAST_EXCLUDES,
    STORAGE_TABLE,
)
from modules.extractors.duplicate_finder.models import (
    DuplicateGroup,
    KeepStrategy,
    ResolveParams,
    ResolveResult,
    ScanParams,
    ScanResult,
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
    ProgressPanel,
    choose_directory,
    confirm_destructive,
    module_header,
    pagination_controls,
    section_card,
    section_title,
    stat_chip,
)

logger = get_logger(__name__)

STRATEGY_LABELS: dict[str, str] = {
    KeepStrategy.NEWEST.value: "Keep newest — delete older copies",
    KeepStrategy.OLDEST.value: "Keep oldest — delete newer copies",
    KeepStrategy.MANUAL.value: "Choose per group",
}

_HELP_TEXT = (
    "Point at a folder and press Scan. Files are grouped first by size, then "
    "by content hash, so only genuine byte-for-byte duplicates are shown. "
    "Pick which copy survives — newest, oldest, or a manual choice per group "
    "— and Run moves the rest to the recycle store, undoable for 24 hours."
)


class DuplicateFinderUI:
    """Renders the Duplicate Detective panel and handles EventBus updates.

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
        self._min_size_bytes = DEFAULT_MIN_SIZE_BYTES
        self._strategy = KeepStrategy.NEWEST
        self._scan: ScanResult | None = None
        self._manual_keep: dict[str, Path] = {}
        self._excluded_groups: set[str] = set()
        self._group_page = 0

        self._dir_input: ui.input | None = None
        self._groups_container: ui.column | None = None
        self._summary_label: ui.label | None = None
        self._run_btn: ui.button | None = None
        self._result_card: ui.card | None = None
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
        """Build the Duplicate Detective UI inside *container*.

        Args:
            container: NiceGUI parent element.
        """
        with ui.column().style("gap: 20px; width: 100%; max-width: 900px;"):
            module_header(
                "Duplicate Detective",
                "Find files with identical content and reclaim the wasted space.",
                _HELP_TEXT,
            )
            self._render_source_card()
            self._render_groups_card()
            self._render_strategy_card()
            self._progress.render(on_cancel=self._on_cancel_click)
            self._errors.render()
            self._render_result_card()

    def _render_source_card(self) -> None:
        """Render the source directory picker, excludes and minimum size."""
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

            with ui.row().style("gap: 16px; width: 100%; margin-top: 12px;"):
                with ui.column().style("gap: 4px; flex: 1;"):
                    ui.label("Exclude patterns (one per line, gitignore style)").style(
                        f"color: {COLOR_TEXT_MUTED}; font-size: 12px;"
                    )
                    ui.textarea(
                        value=self._excludes,
                        on_change=lambda e: setattr(self, "_excludes", e.value or ""),
                    ).props("outlined dense").style(
                        "width: 100%; font-family: monospace; font-size: 12px; height: 90px;"
                    )
                with ui.column().style("gap: 4px; width: 220px;"):
                    ui.label("Minimum file size (bytes)").style(
                        f"color: {COLOR_TEXT_MUTED}; font-size: 12px;"
                    )
                    ui.number(
                        value=self._min_size_bytes,
                        min=0,
                        on_change=lambda e: setattr(
                            self, "_min_size_bytes", int(e.value or 0)
                        ),
                    ).props("outlined dense").style("width: 100%;")

    def _render_groups_card(self) -> None:
        """Render the scanned duplicate groups."""
        with section_card():
            section_title("Duplicate Groups")
            self._summary_label = ui.label("Scan a folder to look for duplicates.").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 12px; margin-bottom: 8px;"
            )
            self._groups_container = ui.column().style(
                f"gap: 10px; width: 100%; max-height: {GROUP_LIST_HEIGHT_PX}px;"
                " overflow-y: auto;"
            )

    def _render_strategy_card(self) -> None:
        """Render the keep-strategy picker and the run button."""
        with section_card():
            section_title("Resolve")
            ui.select(
                options=STRATEGY_LABELS,
                value=self._strategy.value,
                on_change=self._on_strategy_change,
            ).props("outlined dense options-dense").style("width: 100%;")

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

    # ─── Groups ───────────────────────────────────────────────────────────────

    def _refresh_groups(self) -> None:
        """Repaint the duplicate-groups list."""
        if self._groups_container is None:
            return
        self._groups_container.clear()
        with self._groups_container:
            if self._scan is None:
                return
            if not self._scan.groups:
                ui.label("No duplicates found.").style(
                    f"color: {COLOR_POSITIVE}; font-size: 13px; padding: 6px 2px;"
                )
                return
            groups = self._scan.groups
            pages = max(1, (len(groups) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
            self._group_page = max(0, min(self._group_page, pages - 1))
            start = self._group_page * GROUPS_PER_PAGE
            for group in groups[start : start + GROUPS_PER_PAGE]:
                self._render_group_row(group)
            pagination_controls(
                self._group_page,
                len(groups),
                GROUPS_PER_PAGE,
                self._go_to_group_page,
                noun="groups",
            )

    def _go_to_group_page(self, page: int) -> None:
        """Switch the duplicate-groups list to *page* and repaint.

        Selection state (include/exclude and manual keep) is keyed by content
        hash, so it survives paging untouched.

        Args:
            page: The 0-based page to show.
        """
        self._group_page = page
        self._refresh_groups()

    def _render_group_row(self, group: DuplicateGroup) -> None:
        """Render one duplicate group, with a manual keep-picker when needed.

        Args:
            group: The group to render.
        """
        with ui.card().props("flat").style(
            f"background: {COLOR_OVERLAY}; border: 1px solid {COLOR_BORDER};"
            " border-radius: 8px; padding: 10px 14px; width: 100%;"
        ):
            with ui.row().style(
                "align-items: center; justify-content: space-between; width: 100%;"
            ):
                with ui.row().style("align-items: center; gap: 8px;"):
                    ui.checkbox(
                        value=group.content_hash not in self._excluded_groups,
                        on_change=lambda e, h=group.content_hash: self._toggle_group(
                            h, bool(e.value)
                        ),
                    ).props("dense")
                    ui.label(
                        f"{len(group.files)} copies · {format_bytes(group.size_bytes)} each"
                    ).style(
                        f"color: {COLOR_TEXT_MUTED}; font-size: 13px; font-weight: 600;"
                    )
                ui.label(f"reclaims {format_bytes(group.wasted_bytes)}").style(
                    f"color: {COLOR_WARNING}; font-size: 12px;"
                )

            if self._strategy is KeepStrategy.MANUAL:
                current = str(
                    self._manual_keep.get(group.content_hash, group.files[0].path)
                )
                ui.radio(
                    {str(f.path): str(f.path) for f in group.files},
                    value=current,
                    on_change=lambda e, h=group.content_hash: self._manual_keep.__setitem__(
                        h, Path(str(e.value))
                    ),
                ).props("dense").style("margin-top: 4px;")
            else:
                for f in group.files:
                    ui.label(str(f.path)).style(
                        f"color: {COLOR_TEXT_DIM}; font-size: 11px; font-family: monospace;"
                        " word-break: break-all; margin-left: 30px;"
                    )

    def _toggle_group(self, content_hash: str, included: bool) -> None:
        """Include or exclude one group from the next resolve run.

        Args:
            content_hash: The group's identifying hash.
            included: True to include it in the cleanup.
        """
        if included:
            self._excluded_groups.discard(content_hash)
        else:
            self._excluded_groups.add(content_hash)

    def _refresh_summary(self) -> None:
        """Update the scan summary line."""
        if self._summary_label is None:
            return
        if self._scan is None:
            self._summary_label.set_text("Scan a folder to look for duplicates.")
            return
        self._summary_label.set_text(
            f"{self._scan.total_files_scanned:,} files scanned · "
            f"{len(self._scan.groups)} duplicate groups · "
            f"{format_bytes(self._scan.total_wasted_bytes)} reclaimable"
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

    async def _on_scan_click(self) -> None:
        """Validate the source and request a scan."""
        source = self._source_dir.strip()
        if not source:
            ui.notify("Choose a folder to scan.", type="warning")
            return

        try:
            params = ScanParams(
                source_dir=Path(source),
                exclude_patterns=self._exclude_list(),
                min_size_bytes=self._min_size_bytes,
            )
        except ValueError as exc:
            ui.notify(_first_error(exc), type="negative")
            return

        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_DIR, source)
        store_set(STORAGE_TABLE, STORAGE_KEY_LAST_EXCLUDES, self._exclude_list())

        self._progress.update(0, "Scanning…")
        await event_bus.publish(EVENT_SCAN, params)

    def _on_strategy_change(self, event: Any) -> None:
        """Switch the keep strategy and repaint the groups (Manual needs radios)."""
        self._strategy = KeepStrategy(str(event.value))
        self._refresh_groups()

    async def _on_run_click(self) -> None:
        """Validate the selection and start the resolve, confirming first."""
        if self._is_running:
            return
        if self._scan is None:
            ui.notify("Scan a folder first.", type="warning")
            return

        chosen = [
            g for g in self._scan.groups if g.content_hash not in self._excluded_groups
        ]
        if not chosen:
            ui.notify("No duplicate groups selected.", type="warning")
            return

        params = ResolveParams(
            groups=chosen,
            strategy=self._strategy,
            manual_keep=dict(self._manual_keep),
        )

        impact_bytes = sum(g.wasted_bytes for g in chosen)
        impact_files = sum(len(g.files) - 1 for g in chosen)
        await confirm_destructive(
            title="Delete duplicate files?",
            impact=format_impact(impact_bytes, impact_files),
            detail=(
                "One copy is kept per group; the rest are moved to the recycle "
                "store, where they can be restored for 24 hours."
            ),
            confirm_label="Delete Duplicates",
            on_confirm=lambda: self._start(params),
        )

    async def _start(self, params: ResolveParams) -> None:
        """Publish the execute request and lock the run button.

        Args:
            params: The validated resolve parameters.
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

    async def _on_scanned(self, payload: Any) -> None:
        """Populate the groups list from a completed scan.

        Args:
            payload: A ScanResult.
        """
        if not isinstance(payload, ScanResult):
            return
        self._scan = payload
        self._manual_keep = {}
        self._excluded_groups = set()
        self._group_page = 0
        self._refresh_groups()
        self._refresh_summary()
        self._progress.reset(
            f"Found {len(payload.groups)} duplicate groups in "
            f"{payload.scan_duration_ms:.0f} ms."
        )

    async def _on_progress(self, payload: Any) -> None:
        """Advance the progress panel.

        Args:
            payload: A ProgressEvent.
        """
        if isinstance(payload, ProgressEvent):
            self._progress.update(payload.percent, payload.message)

    async def _on_done(self, payload: Any) -> None:
        """Render the result card once a resolve completes.

        Args:
            payload: A ResolveResult.
        """
        if not isinstance(payload, ResolveResult):
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
        self._errors.show("The resolve could not be completed.", message)
        logger.error("duplicate_finder.ui.error — %s", message)

    def _reset_controls(self) -> None:
        """Return the run button to its idle state."""
        self._is_running = False
        if self._run_btn is not None:
            self._run_btn.set_text("Run")

    def _render_result(self, result: ResolveResult) -> None:
        """Populate the result card.

        Args:
            result: The completed run's result.
        """
        with ui.grid(columns=2).style("gap: 16px; width: 100%;"):
            stat_chip("🗑️ Deleted", f"{result.files_deleted:,}")
            # Not "Reclaimed" — the bytes are still on disk holding the undo
            # window open, and free space has not moved yet.
            stat_chip("📦 Recoverable", format_bytes(result.bytes_pending_release))

        if result.recycle_batch_id is not None:
            with ui.row().style(
                f"align-items: center; gap: 8px; margin-top: 12px; padding: 10px 14px;"
                f" background: {COLOR_OVERLAY}; border-radius: 6px; width: 100%;"
            ):
                ui.icon("restore_from_trash").style(
                    f"color: {COLOR_WARNING}; font-size: 18px;"
                )
                ui.label(
                    "Deleted files moved to the recycle store — restorable for 24 "
                    f"hours (batch {result.recycle_batch_id}). That space is released "
                    "when the batch expires, or now via the Recycle Bin in the header."
                ).style(f"color: {COLOR_TEXT_MUTED}; font-size: 11px; flex: 1;")

        for note in result.warnings:
            ui.label(f"⚠ {note}").style(
                f"color: {COLOR_NEGATIVE}; font-size: 11px; margin-top: 6px;"
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
