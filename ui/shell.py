"""OmniForge application shell — header, sidebar, and content area.

Renders the persistent chrome that wraps all module UIs.
The shell is stateless beyond which module is currently active.
Module UIs are mounted into the content column via ``module.build_ui()``.

Architecture notes:
- The shell imports BaseModule only for type hints — it never calls logic directly.
- Module switching is triggered by sidebar clicks, not the EventBus, because
  navigation is a UI concern, not an inter-module concern.
"""
from __future__ import annotations

import asyncio
import datetime
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from nicegui import ui
from nicegui.events import ValueChangeEventArguments

from core.command_palette import (
    CommandPalette,
    build_entries,
    load_recent,
    record_recent,
)
from core.logger import get_logger
from core.models import ModuleMetadata
from core.recycle_store import (
    RecycleBatch,
    delete_batch,
    list_batches,
    restore_batch,
)
from core.theme_engine import Theme, apply_theme, inject_theme, is_dark, load_theme
from shared.constants import (
    APP_TITLE,
    COLOR_BORDER,
    COLOR_CARD_BG,
    COLOR_DARK_BG,
    COLOR_NEGATIVE,
    COLOR_PRIMARY,
    COLOR_PRIMARY_SOFT,
    COLOR_SIDEBAR_BG,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
    COLOR_WARNING,
    OS_ICONS,
    SIDEBAR_WIDTH_PX,
)
from shared.formatters import format_bytes
from shared.platform_info import describe_host
from shared.ui_components import show_dialog

if TYPE_CHECKING:
    from core.base_module import BaseModule

logger = get_logger(__name__)

_MINUTES_PER_HOUR = 60


def _relative(moment: datetime.datetime) -> str:
    """Describe how far *moment* is from now, in words.

    Args:
        moment: A timezone-aware instant.

    Returns:
        Text such as ``"in 6h 12m"`` or ``"expired"``.
    """
    delta = moment - datetime.datetime.now(datetime.UTC)
    total_minutes = int(delta.total_seconds() // _MINUTES_PER_HOUR)
    if total_minutes <= 0:
        return "expired"
    hours, minutes = divmod(total_minutes, _MINUTES_PER_HOUR)
    return f"in {hours}h {minutes}m" if hours else f"in {minutes}m"


def sidebar_item_style(is_active: bool) -> str:
    """Return the CSS for a sidebar row in its active or inactive state.

    Args:
        is_active: Whether this row's module is the one on screen.

    Returns:
        A CSS declaration string.
    """
    accent = (
        f"border-left: 2px solid {COLOR_PRIMARY};"
        f" background-color: {COLOR_PRIMARY_SOFT};"
        if is_active
        else "border-left: 2px solid transparent;"
    )
    return (
        "align-items: center; gap: 8px; padding: 8px 14px; cursor: pointer;"
        f" border-radius: 6px; margin: 1px 8px; {accent}"
    )


def sidebar_label_style(is_active: bool) -> str:
    """Return the CSS for a sidebar label in its active or inactive state.

    Args:
        is_active: Whether this row's module is the one on screen.

    Returns:
        A CSS declaration string.
    """
    colour = COLOR_TEXT_PRIMARY if is_active else COLOR_TEXT_MUTED
    return f"color: {colour}; font-size: 13px;"


class Shell:
    """Top-level NiceGUI shell: header + sidebar + scrollable content area.

    Args:
        modules: Loaded module instances from the Registry.
        degraded: Dict of module_id → failure reason for DEGRADED modules.
    """

    def __init__(
        self,
        modules: list[BaseModule],
        degraded: dict[str, str] | None = None,
        metadata: list[ModuleMetadata] | None = None,
    ) -> None:
        self._modules = modules
        self._degraded = degraded or {}
        self._active_module: BaseModule | None = None
        self._content: ui.column | None = None
        self._sidebar_items: dict[str, ui.row] = {}
        self._sidebar_labels: dict[str, ui.label] = {}
        self._theme: Theme = load_theme()
        self._dark_mode = ui.dark_mode(value=is_dark(self._theme))
        self._palette = CommandPalette(on_activate=self._activate_by_id)
        self._palette.set_entries(build_entries(metadata or [], load_recent()))

    # ─── Public ───────────────────────────────────────────────────────────────

    def render(self) -> None:
        """Build and mount the full application shell in NiceGUI."""
        # Publish the active palette before any styled element is created.
        inject_theme(self._theme)

        ui.query("body").style(f"background-color: {COLOR_DARK_BG}; margin: 0; padding: 0;")
        ui.query(".nicegui-content").style("padding: 0;")

        with ui.header().style(
            f"background-color: {COLOR_SIDEBAR_BG}; height: 56px;"
            f" padding: 0 20px; display: flex; align-items: center;"
            f" justify-content: space-between; border-bottom: 1px solid {COLOR_BORDER};"
            " box-shadow: 0 1px 8px rgba(0,0,0,0.4); z-index: 100;"
        ):
            self._render_header()

        with ui.left_drawer(value=True, fixed=True).style(
            f"background-color: {COLOR_SIDEBAR_BG}; width: {SIDEBAR_WIDTH_PX}px;"
            f" border-right: 1px solid {COLOR_BORDER}; padding: 8px 0;"
            " overflow-y: auto;"
        ):
            self._render_sidebar()

        with ui.column().classes("w-full h-full").style(
            f"background-color: {COLOR_DARK_BG}; padding: 28px; min-height: 100vh;"
            " box-sizing: border-box;"
        ) as self._content:
            self._render_welcome()

        # Mounted last so the palette overlays every other element.
        self._palette.render()

    # ─── Header ───────────────────────────────────────────────────────────────

    def _render_header(self) -> None:
        """Render the top navigation bar with logo and search hint."""
        with ui.row().style("align-items: center; gap: 10px;"):
            ui.label("⚒").style("font-size: 20px;")
            ui.label(APP_TITLE).style(
                f"color: {COLOR_PRIMARY}; font-size: 17px; font-weight: 700;"
                " letter-spacing: 0.4px;"
            )

        with ui.row().style("align-items: center; gap: 8px;"):
            ui.button(
                "Ctrl+K  Search modules",
                on_click=self._palette.open,
            ).props("flat dense no-caps").style(
                f"color: {COLOR_TEXT_MUTED}; font-size: 12px;"
                f" border: 1px solid {COLOR_BORDER}; border-radius: 6px;"
                " padding: 4px 14px; background: transparent;"
            )
            self._render_recycle_button()
            self._render_host_badge()
            self._render_theme_switcher()

    def _render_recycle_button(self) -> None:
        """Header entry point for the Recycle Bin (rule B-04's undo window).

        Destructive operations move files into ``core.recycle_store`` and keep
        them for 24 hours, but nothing in the app ever exposed them — the undo
        window existed on disk and was unreachable. See RFC 0003.
        """
        ui.button(icon="restore_from_trash", on_click=self._open_recycle_bin).props(
            "flat dense round"
        ).style(f"color: {COLOR_TEXT_MUTED};").tooltip(
            "Recycle Bin — restore files from destructive operations"
        )

    async def _open_recycle_bin(self) -> None:
        """Build and show the Recycle Bin dialog, listing every held batch.

        Every store operation runs on a worker thread: listing reads a manifest
        per batch, restoring moves the payload back (potentially gigabytes) and
        deleting is a recursive removal. Running those inline froze the whole
        application — the one place in the app most likely to be handling a
        large amount of data (rule B-01).
        """
        with ui.dialog() as dialog, ui.card().props("flat").style(
            f"background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER};"
            " border-radius: 12px; padding: 0; width: 640px; max-width: 94vw;"
        ):
            with ui.column().style(
                f"gap: 2px; width: 100%; padding: 16px 20px;"
                f" border-bottom: 1px solid {COLOR_BORDER};"
            ):
                ui.label("Recycle Bin").style(
                    f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: 700;"
                )
                ui.label(
                    "Files moved here by destructive operations. Kept for 24 hours."
                ).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")

            body = ui.column().style(
                "gap: 0; width: 100%; padding: 6px; max-height: 380px;"
                " overflow-y: auto;"
            )

            with ui.row().style(
                f"justify-content: flex-end; width: 100%; padding: 12px 20px;"
                f" border-top: 1px solid {COLOR_BORDER};"
            ):
                ui.button("Close", on_click=dialog.close).props("flat no-caps").style(
                    f"color: {COLOR_TEXT_MUTED};"
                )

        async def repaint() -> None:
            """Redraw the batch list from the store's current contents."""
            batches = await asyncio.to_thread(list_batches)
            body.clear()
            with body:
                if not batches:
                    ui.label("Nothing here — no recoverable files.").style(
                        f"color: {COLOR_TEXT_DIM}; font-size: 13px; padding: 20px;"
                    )
                    return
                for batch in batches:
                    self._render_batch_row(batch, repaint)

        await repaint()
        dialog.open()
        await show_dialog(dialog)

    def _render_batch_row(
        self, batch: RecycleBatch, repaint: Callable[[], Awaitable[None]]
    ) -> None:
        """Render one recycle batch with Restore and Delete controls.

        Args:
            batch: The batch to display.
            repaint: Awaitable that redraws the list after an action.
        """
        confirming = {"delete": False}

        with ui.row().style(
            f"align-items: center; gap: 10px; width: 100%; padding: 9px 14px;"
            f" border-bottom: 1px solid {COLOR_BORDER};"
        ):
            with ui.column().style("gap: 1px; flex: 1;"):
                noun = "item" if batch.entry_count == 1 else "items"
                ui.label(f"{batch.entry_count} {noun} · {format_bytes(batch.total_bytes)}").style(
                    f"color: {COLOR_TEXT_PRIMARY}; font-size: 13px; font-weight: 500;"
                )
                ui.label(f"{batch.batch_id} · expires {_relative(batch.expires_at())}").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 11px; font-family: monospace;"
                )

            async def on_restore(_e: object = None) -> None:
                """Move every entry in this batch back where it came from."""
                # Off the loop: a batch can hold gigabytes, and the move may
                # cross volumes, which turns it into a full copy.
                count = await asyncio.to_thread(restore_batch, batch.batch_id)
                ui.notify(
                    f"Restored {count} item(s)." if count else "Nothing could be restored.",
                    type="positive" if count else "warning",
                )
                await repaint()

            async def on_delete(_e: object = None) -> None:
                """Delete permanently, behind a two-step confirmation (E-08)."""
                if not confirming["delete"]:
                    confirming["delete"] = True
                    delete_button.set_text("Confirm?")
                    delete_button.style(f"color: {COLOR_NEGATIVE}; font-weight: 700;")
                    return
                await asyncio.to_thread(delete_batch, batch.batch_id)
                ui.notify("Batch deleted permanently.", type="warning")
                await repaint()

            ui.button("Restore", on_click=on_restore).props("flat dense no-caps").style(
                f"color: {COLOR_PRIMARY};"
            )
            delete_button = (
                ui.button("Delete", on_click=on_delete)
                .props("flat dense no-caps")
                .style(f"color: {COLOR_TEXT_MUTED};")
            )

    def _render_host_badge(self) -> None:
        """Show the detected operating system, so platform behaviour is visible."""
        host = describe_host()
        with ui.row().style(
            f"align-items: center; gap: 6px; padding: 4px 10px;"
            f" border: 1px solid {COLOR_BORDER}; border-radius: 6px;"
        ):
            ui.icon(OS_ICONS.get(host.os.value, "computer")).style(
                f"color: {COLOR_TEXT_DIM}; font-size: 15px;"
            )
            ui.label(host.short_label).style(
                f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
            )
            ui.tooltip(
                f"{host.display_name} {host.release} ({host.version})\n"
                f"Architecture: {host.architecture}\n"
                f"Python {host.python_version}\n"
                f"Host: {host.hostname}"
            ).style("white-space: pre-line; font-size: 11px;")

    def _render_theme_switcher(self) -> None:
        """Render the theme selector (rule E-07 — applies without restart)."""
        ui.select(
            options={theme.value: theme.value.title() for theme in Theme},
            value=self._theme.value,
            on_change=self._on_theme_change,
        ).props("outlined dense options-dense borderless").style(
            f"color: {COLOR_TEXT_MUTED}; font-size: 12px; min-width: 110px;"
        ).tooltip("Switch colour theme")

    def _on_theme_change(self, event: ValueChangeEventArguments[str]) -> None:
        """Apply and persist the newly selected theme.

        Args:
            event: NiceGUI change event carrying the selected theme value.
        """
        self._theme = Theme(str(event.value))
        self._dark_mode.value = is_dark(self._theme)
        apply_theme(self._theme)
        logger.info("shell.theme_changed — theme=%s", self._theme.value)

    # ─── Sidebar ──────────────────────────────────────────────────────────────

    def _render_sidebar(self) -> None:
        """Render pillar headings and module navigation items."""
        if not self._modules and not self._degraded:
            ui.label("No modules loaded").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 16px 14px;"
            )
            return

        # Group by pillar
        pillars: dict[str, list[BaseModule]] = {}
        for module in self._modules:
            pillars.setdefault(module.pillar, []).append(module)

        for pillar_key in sorted(pillars):
            pillar_label = pillar_key.replace("_", " ").title()
            ui.label(pillar_label).style(
                f"color: {COLOR_TEXT_DIM}; font-size: 10px; font-weight: 700;"
                " letter-spacing: 1px; padding: 14px 14px 4px;"
                " text-transform: uppercase;"
            )
            for module in pillars[pillar_key]:
                self._render_sidebar_item(module)

        # Degraded modules
        if self._degraded:
            ui.separator().style(f"background: {COLOR_BORDER}; margin: 8px 14px;")
            ui.label("Degraded").style(
                f"color: {COLOR_WARNING}; font-size: 10px; font-weight: 700;"
                " letter-spacing: 1px; padding: 4px 14px; text-transform: uppercase;"
            )
            for mid, reason in self._degraded.items():
                with ui.tooltip(reason):
                    ui.label(f"⚠ {mid}").style(
                        f"color: {COLOR_WARNING}; font-size: 12px; padding: 6px 14px;"
                        " opacity: 0.7;"
                    )

    def _render_sidebar_item(self, module: BaseModule) -> None:
        """Render a single clickable sidebar nav item.

        Args:
            module: The module to create a sidebar entry for.
        """
        is_active = self._active_module is not None and (
            self._active_module.module_id == module.module_id
        )

        with ui.row().style(sidebar_item_style(is_active)) as row:
            self._sidebar_items[module.module_id] = row
            label = ui.label(module.name).style(sidebar_label_style(is_active))
            self._sidebar_labels[module.module_id] = label
            row.on("click", lambda m=module: self._activate_module(m))

    def _repaint_sidebar_selection(self) -> None:
        """Restyle every nav item so the active one stands out.

        ``_sidebar_items`` was populated at render time and then never read, so
        the highlight was computed once — when nothing was active yet — and no
        item ever appeared selected however the user navigated.
        """
        active_id = self._active_module.module_id if self._active_module else None
        for module_id, row in self._sidebar_items.items():
            row.style(sidebar_item_style(module_id == active_id))
        for module_id, label in self._sidebar_labels.items():
            label.style(sidebar_label_style(module_id == active_id))

    # ─── Content Area ─────────────────────────────────────────────────────────

    def _activate_by_id(self, module_id: str) -> None:
        """Activate a module by ID — the entry point used by the palette.

        Args:
            module_id: Dot-separated identifier of the module to open.
        """
        for module in self._modules:
            if module.module_id == module_id:
                self._activate_module(module)
                return
        logger.warning("shell.unknown_module — module=%s", module_id)

    def _deactivate(self, module: BaseModule) -> None:
        """Schedule the outgoing module's ``on_deactivate`` without blocking nav.

        Navigation is synchronous (reached from a sync palette callback and
        click handlers), so the coroutine is scheduled on the running loop
        rather than awaited. ``on_deactivate`` is contracted not to raise, but
        the guard keeps a misbehaving module from breaking navigation. See
        RFC 0002.

        Args:
            module: The module being navigated away from.
        """
        async def _run() -> None:
            try:
                await module.on_deactivate()
            except Exception as exc:
                logger.error(
                    "shell.deactivate_failed — module=%s", module.module_id, exc_info=exc
                )

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            # No running loop (e.g. a unit test driving navigation directly).
            logger.debug("shell.deactivate_no_loop — module=%s", module.module_id)

    def _activate_module(self, module: BaseModule) -> None:
        """Switch the content area to render *module*.

        Args:
            module: Module to activate.
        """
        if self._content is None:
            return

        # Tell the module we are leaving so it can stop any real-time work
        # (e.g. live_monitor's polling) instead of leaving it running against
        # a UI we are about to discard. See RFC 0002.
        outgoing = self._active_module
        if outgoing is not None and outgoing.module_id != module.module_id:
            self._deactivate(outgoing)

        self._active_module = module
        self._repaint_sidebar_selection()
        record_recent(module.module_id)
        self._content.clear()

        with self._content:
            try:
                module.build_ui(self._content)
            except Exception as exc:
                logger.error(
                    "shell.module_ui_failed — module=%s", module.module_id, exc_info=exc
                )
                with ui.column().style("gap: 8px;"):
                    ui.label(f"⚠ Failed to load {module.name}").style(
                        f"color: {COLOR_WARNING}; font-size: 16px; font-weight: 600;"
                    )
                    ui.label(str(exc)).style(f"color: {COLOR_TEXT_MUTED}; font-size: 13px;")

        logger.info("shell.activated — module=%s", module.module_id)

    def _render_welcome(self) -> None:
        """Render the welcome screen before any module is selected."""
        with ui.column().style(
            "align-items: center; justify-content: center; flex: 1; gap: 20px;"
            " min-height: 70vh; text-align: center;"
        ):
            ui.label("⚒").style("font-size: 72px; line-height: 1;")
            ui.label(APP_TITLE).style(
                f"color: {COLOR_PRIMARY}; font-size: 40px; font-weight: 800;"
                " letter-spacing: -0.5px;"
            )
            ui.label("Developer's Unified Automation Forge").style(
                f"color: {COLOR_TEXT_MUTED}; font-size: 15px;"
            )
            ui.separator().style(
                f"width: 40px; background: {COLOR_PRIMARY}; height: 2px; margin: 4px auto;"
            )
            ui.label("Select a module from the sidebar to begin.").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 14px;"
            )

            if self._modules:
                with ui.row().style("gap: 12px; flex-wrap: wrap; justify-content: center;"):
                    for module in self._modules[:4]:
                        with ui.card().style(
                            f"background: {COLOR_SIDEBAR_BG}; border: 1px solid {COLOR_BORDER};"
                            " border-radius: 8px; padding: 12px 18px; cursor: pointer;"
                            " min-width: 140px; text-align: center;"
                        ).on("click", lambda m=module: self._activate_module(m)):
                            ui.label(module.name).style(
                                f"color: {COLOR_TEXT_PRIMARY}; font-size: 13px; font-weight: 500;"
                            )
