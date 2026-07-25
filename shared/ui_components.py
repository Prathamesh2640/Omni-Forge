"""Reusable NiceGUI components shared by every module UI.

Centralising these keeps the UI rules satisfied everywhere by construction:

- :func:`help_tooltip`   — rule E-02 (every module UI explains itself).
- :class:`ErrorPanel`    — rule E-04 (expandable full error + copy button).
- :class:`ProgressPanel` — rule E-01 (feedback within 2 seconds).
- :func:`confirm_destructive` — rules B-03 and E-08 (impact summary,
  two-step amber/red confirmation).

These render UI only; they never import module logic (rule A-03).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

import psutil
from nicegui import ui

from shared.constants import (
    BINARY_PREVIEW_NOTICE,
    BINARY_SNIFF_BYTES,
    CLIPBOARD_MAX_CHARS,
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_CARD_BG,
    COLOR_NEGATIVE,
    COLOR_NEGATIVE_SOFT,
    COLOR_OVERLAY,
    COLOR_POSITIVE,
    COLOR_PRIMARY,
    COLOR_TEXT_DIM,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_PRIMARY,
    COLOR_WARNING,
    DIRECTORY_PICKER_LIST_HEIGHT_PX,
    DIRECTORY_PICKER_MAX_ENTRIES,
    DIRECTORY_PICKER_WIDTH_PX,
    EXPORTS_DIR,
    FILE_PREVIEW_HEIGHT_PX,
    FILE_PREVIEW_MAX_CHARS,
    FILE_PREVIEW_TRUNCATION_NOTICE,
    FILE_PREVIEW_WIDTH_PX,
    FILE_TYPE_ALL,
    LINUX_FILE_MANAGER,
    LOG_ROOT_NAME,
    MACOS_FILE_MANAGER,
    NATIVE_DIALOG_FOLDER,
    NATIVE_DIALOG_OPEN,
    NATIVE_DIALOG_SAVE,
    WINDOWS_FILE_MANAGER,
)
from shared.formatters import format_bytes
from shared.platform_info import is_macos, is_windows

# ``shared`` sits below ``core`` in the dependency order (rule A-07), so this
# attaches to the same logger tree by name rather than importing core.logger.
logger = logging.getLogger(f"{LOG_ROOT_NAME}.shared.ui_components")

#: A ProgressEvent at this percentage means the operation has finished.
PERCENT_COMPLETE: int = 100

_CARD_STYLE = (
    f"background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER};"
    " border-radius: 10px; padding: 18px 22px; width: 100%;"
)


#: Key used when no NiceGUI client context is active (unit tests, direct calls).
NO_CLIENT_KEY: str = "__no_client__"


def client_key() -> str:
    """Return an identifier for the browser client currently rendering.

    Module UIs hold element references, so one controller per module is only
    correct while exactly one client exists. Keying them by client is what lets
    a second tab render without stealing the first tab's handlers (audit §3.9).

    Returns:
        The active client's id, or :data:`NO_CLIENT_KEY` outside a client
        context.
    """
    try:
        from nicegui import context

        return str(context.client.id)
    except Exception as exc:
        # No client context — a unit test, or a call outside a page handler.
        logger.debug("ui.no_client_context — reason=%s", exc)
        return NO_CLIENT_KEY


def on_client_disconnect(callback: Callable[[], None]) -> None:
    """Run *callback* when the rendering client goes away.

    Used to release a module UI's EventBus subscriptions when its tab closes,
    so handlers do not accumulate against elements that no longer exist.

    Args:
        callback: Invoked once on disconnect. Failures are swallowed — teardown
            must never break the disconnect path.
    """
    try:
        from nicegui import context

        context.client.on_disconnect(callback)
    except Exception as exc:
        logger.debug("ui.disconnect_hook_unavailable — reason=%s", exc)


def copy_to_clipboard(text: str) -> None:
    """Copy *text* to the system clipboard via the browser API.

    The value is JSON-encoded before interpolation so quotes, backslashes and
    newlines in error messages or paths cannot break out of the JS string.

    Args:
        text: The text to place on the clipboard.
    """
    ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(text)})")


async def show_dialog(dialog: ui.dialog) -> Any:
    """Await a dialog's result, then remove it from the client element tree.

    A dialog built inside a click handler is a *new* element every time the
    handler runs, parented to the page and never garbage-collected while the
    tab lives. Browsing for files a few dozen times therefore accumulated a few
    dozen dialogs, each holding its own listeners — and a file preview holds up
    to 200 000 characters of text with it. Awaiting and disposing here keeps a
    transient dialog transient.

    Args:
        dialog: A dialog that has just been built.

    Returns:
        Whatever the dialog was submitted with, or ``None`` when dismissed.
    """
    try:
        return await dialog
    finally:
        dialog.clear()
        dialog.delete()


def section_card() -> ui.card:
    """Create the standard flat card used to group a section of a module UI.

    Returns:
        A NiceGUI card, intended for use as a context manager.
    """
    return ui.card().props("flat").style(_CARD_STYLE)


def section_title(text: str) -> ui.label:
    """Render the small uppercase heading used above a card's contents.

    Args:
        text: Heading text.

    Returns:
        The created label.
    """
    return ui.label(text).style(
        f"color: {COLOR_TEXT_MUTED}; font-size: 11px; font-weight: 700;"
        " text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 12px;"
    )


def help_tooltip(text: str) -> None:
    """Render the help affordance required on every module UI (rule E-02).

    Args:
        text: Explanation of the module's purpose, inputs and output.
    """
    with ui.icon("help_outline").style(
        f"color: {COLOR_TEXT_DIM}; font-size: 18px; cursor: help;"
    ):
        ui.tooltip(text).style("font-size: 12px; max-width: 320px;")


def module_header(title: str, subtitle: str, help_text: str) -> None:
    """Render the standard module title block with its help tooltip.

    Args:
        title: Module display name.
        subtitle: One-line description shown beneath the title.
        help_text: Tooltip body explaining purpose, inputs and output.
    """
    with ui.row().style("align-items: center; gap: 10px; width: 100%;"):
        with ui.column().style("gap: 2px;"):
            ui.label(title).style(
                f"color: {COLOR_TEXT_PRIMARY}; font-size: 22px; font-weight: 700;"
            )
            ui.label(subtitle).style(f"color: {COLOR_TEXT_DIM}; font-size: 13px;")
        help_tooltip(help_text)


class ProgressPanel:
    """Progress bar plus status line, updated from ProgressEvents (rule E-01).

    Usage::

        panel = ProgressPanel()
        panel.render()
        panel.update(percent=40, message="Reading 4/10…")
    """

    def __init__(self) -> None:
        self._bar: ui.linear_progress | None = None
        self._status: ui.label | None = None
        self._cancel: ui.button | None = None

    def render(self, on_cancel: Callable[[], Awaitable[None]] | None = None) -> None:
        """Build the progress card.

        Args:
            on_cancel: Awaited when the user asks to stop the operation. When
                given, a Cancel button appears for as long as work is running —
                rule B-02's cancellation is only meaningful if the user can
                actually reach it (see RFC 0003).
        """
        with section_card():
            # Quasar's `color` prop only accepts palette names; the themed
            # colour reaches the bar through currentColor instead.
            self._bar = (
                ui.linear_progress(value=0.0, show_value=False)
                .props("rounded")
                .style(f"width: 100%; color: {COLOR_PRIMARY};")
            )
            with ui.row().style(
                "align-items: center; gap: 10px; width: 100%; margin-top: 8px;"
            ):
                self._status = ui.label("Ready.").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 13px; flex: 1;"
                )
                if on_cancel is not None:
                    self._cancel = (
                        ui.button("Cancel", on_click=on_cancel)
                        .props("flat dense no-caps")
                        .style(f"color: {COLOR_WARNING};")
                    )
                    self._cancel.set_visibility(False)

    def update(self, percent: int, message: str) -> None:
        """Advance the bar and status text.

        The Cancel button follows the work automatically: visible while the
        operation is in flight, hidden once it reaches 100%.

        Args:
            percent: Completion percentage, 0-100.
            message: Status text to display.
        """
        if self._bar is not None:
            self._bar.set_value(percent / 100.0)
        if self._status is not None:
            colour = COLOR_POSITIVE if percent >= 100 else COLOR_TEXT_DIM
            self._status.set_text(message)
            self._status.style(f"color: {colour}; font-size: 13px; flex: 1;")
        if self._cancel is not None:
            self._cancel.set_visibility(percent < PERCENT_COMPLETE)

    def reset(self, message: str = "Ready.") -> None:
        """Return the panel to its idle state.

        Args:
            message: Status text to show while idle.
        """
        if self._bar is not None:
            self._bar.set_value(0.0)
        if self._status is not None:
            self._status.set_text(message)
            self._status.style(f"color: {COLOR_TEXT_DIM}; font-size: 13px; flex: 1;")
        if self._cancel is not None:
            self._cancel.set_visibility(False)


class OutputDirectoryPicker:
    """Lets the user choose where a module writes its output.

    Every converter used to hard-code ``exports/``, so results could only ever
    land in one place. This offers a destination, remembers the last one per
    module (rule E-03), and hands it to the logic layer, which confines writes
    to it (rule B-07).

    Persistence is injected rather than done here: ``shared`` sits below
    ``core`` in the dependency order (rule A-07), so it cannot reach
    ``core.storage`` itself. The calling module UI, which may, supplies the
    stored value and a callback to save the new one.

    Args:
        initial: The destination to start from — normally the module's
            last-used directory, falling back to ``exports/``.
        on_change: Invoked with each newly chosen directory, for the caller to
            persist.
    """

    def __init__(
        self, initial: str = "", on_change: Callable[[str], None] | None = None
    ) -> None:
        self._value = initial or EXPORTS_DIR
        self._on_change = on_change
        self._input: ui.input | None = None

    @property
    def path(self) -> Path:
        """The chosen destination directory."""
        return Path(self._value)

    def render(self) -> None:
        """Render the destination row inside the caller's current container."""
        with ui.row().style("align-items: center; gap: 8px; width: 100%;"):
            self._input = (
                ui.input(
                    label="Output folder",
                    value=self._value,
                    on_change=lambda event: self._set(str(event.value or "")),
                )
                .props("outlined dense")
                .style("flex: 1;")
            )
            ui.button("Browse", on_click=self._browse).props("flat dense no-caps").style(
                f"color: {COLOR_TEXT_MUTED}; border: 1px solid {COLOR_BORDER};"
                " border-radius: 6px;"
            )
            ui.button(icon="restart_alt", on_click=self._reset).props(
                "flat dense round"
            ).style(f"color: {COLOR_TEXT_DIM};").tooltip("Back to the default exports folder")

    def _set(self, value: str) -> None:
        """Record a new destination and hand it to the caller to persist."""
        self._value = value or EXPORTS_DIR
        if self._on_change is not None:
            self._on_change(self._value)

    async def _browse(self) -> None:
        """Open the folder picker at the current destination."""
        chosen = await choose_directory(self.path if self._value else None)
        if chosen is None:
            return
        self._set(str(chosen))
        if self._input is not None:
            self._input.set_value(self._value)

    def _reset(self) -> None:
        """Return the destination to the application's exports folder."""
        self._set(EXPORTS_DIR)
        if self._input is not None:
            self._input.set_value(self._value)


class ErrorPanel:
    """Error display with an expandable full error and a copy button (rule E-04).

    Hidden until :meth:`show` is called.
    """

    def __init__(self) -> None:
        self._container: ui.column | None = None
        self._detail: str = ""

    def render(self) -> None:
        """Build the (initially hidden) error container."""
        self._container = ui.column().style("gap: 0; width: 100%;")
        self._container.set_visibility(False)

    def show(self, summary: str, detail: str = "") -> None:
        """Display an error.

        Args:
            summary: Short, user-readable failure description.
            detail: Full error text or traceback for the expandable section.
                Falls back to *summary* when empty.
        """
        if self._container is None:
            return
        self._detail = detail or summary

        self._container.clear()
        self._container.set_visibility(True)
        with self._container, ui.card().props("flat").style(
            f"background: {COLOR_NEGATIVE_SOFT}; border: 1px solid {COLOR_NEGATIVE};"
            " border-radius: 10px; padding: 16px 20px; width: 100%;"
        ):
            with ui.row().style("align-items: center; gap: 8px; width: 100%;"):
                ui.icon("error_outline").style(
                    f"color: {COLOR_NEGATIVE}; font-size: 20px;"
                )
                ui.label(summary).style(
                    f"color: {COLOR_NEGATIVE}; font-size: 14px; font-weight: 600; flex: 1;"
                )
                ui.button(
                    "Copy to Clipboard",
                    on_click=lambda: copy_to_clipboard(self._detail),
                ).props("flat dense no-caps").style(f"color: {COLOR_TEXT_MUTED};")

            with ui.expansion("View Full Error").props("dense").style(
                f"color: {COLOR_TEXT_MUTED}; font-size: 12px; width: 100%;"
            ):
                ui.label(self._detail).style(
                    f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
                    " font-family: monospace; white-space: pre-wrap;"
                    " word-break: break-word;"
                )

    def hide(self) -> None:
        """Clear and hide the panel."""
        if self._container is None:
            return
        self._container.clear()
        self._container.set_visibility(False)


async def confirm_destructive(
    title: str,
    impact: str,
    detail: str,
    confirm_label: str,
    on_confirm: Callable[[], Awaitable[None]],
) -> None:
    """Show the two-step confirmation required before a destructive action.

    Satisfies rule B-03 (computed impact summary shown before proceeding) and
    rule E-08 (destructive control is amber/red and needs explicit
    confirmation). The dialog is dismissed before *on_confirm* runs.

    Args:
        title: Dialog heading, e.g. ``"Delete 312 duplicate files?"``.
        impact: Computed impact summary from
            :func:`shared.formatters.format_impact`.
        detail: Additional explanation, such as the undo window.
        confirm_label: Text of the confirming button.
        on_confirm: Coroutine invoked only when the user confirms.
    """
    with ui.dialog() as dialog, ui.card().style(
        f"background: {COLOR_CARD_BG}; border: 1px solid {COLOR_WARNING};"
        " border-radius: 10px; padding: 24px; min-width: 420px;"
    ):
        with ui.row().style("align-items: center; gap: 10px;"):
            ui.icon("warning_amber").style(f"color: {COLOR_WARNING}; font-size: 24px;")
            ui.label(title).style(
                f"color: {COLOR_TEXT_PRIMARY}; font-size: 17px; font-weight: 700;"
            )

        ui.label(impact).style(
            f"color: {COLOR_WARNING}; font-size: 15px; font-weight: 600;"
            " margin-top: 8px; font-family: monospace;"
        )
        ui.label(detail).style(
            f"color: {COLOR_TEXT_MUTED}; font-size: 13px; margin-top: 4px;"
        )

        with ui.row().style("justify-content: flex-end; gap: 8px; margin-top: 20px;"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps").style(
                f"color: {COLOR_TEXT_MUTED};"
            )
            ui.button(confirm_label, on_click=lambda: dialog.submit(True)).props(
                "no-caps"
            ).style(
                f"background: {COLOR_NEGATIVE}; color: white;"
                " font-weight: 600; border-radius: 6px;"
            )

    if await show_dialog(dialog):
        await on_confirm()


def drive_roots() -> list[Path]:
    """Return the filesystem roots available on this machine.

    Returns:
        Mount points (drive letters on Windows, mount paths on POSIX).
        Unreadable partitions are omitted rather than raising.
    """
    roots: list[Path] = []
    try:
        for partition in psutil.disk_partitions(all=False):
            candidate = Path(partition.mountpoint)
            if candidate.is_dir():
                roots.append(candidate)
    except OSError as exc:
        logger.warning("ui.drive_enumeration_failed — %s", exc)
    return roots or [Path(Path.cwd().anchor or "/")]


def subdirectories(path: Path) -> list[Path]:
    """List the immediate subdirectories of *path*, sorted by name.

    Entries that cannot be inspected (permission denied, broken links) are
    skipped, so a single unreadable child never empties the listing.

    Args:
        path: Directory to list.

    Returns:
        Up to :data:`~shared.constants.DIRECTORY_PICKER_MAX_ENTRIES`
        subdirectories.
    """
    found: list[Path] = []
    try:
        for entry in path.iterdir():
            try:
                if entry.is_dir():
                    found.append(entry)
            except OSError:
                continue
            if len(found) >= DIRECTORY_PICKER_MAX_ENTRIES:
                break
    except OSError as exc:
        logger.debug("ui.listing_failed — path=%s reason=%s", path, exc)
        return []
    found.sort(key=lambda item: item.name.lower())
    return found


def native_window() -> Any | None:
    """Return the pywebview window proxy, or ``None`` in browser mode.

    NiceGUI runs pywebview in a *separate process*, so ``webview.windows`` is
    always empty inside the server process. ``app.native.main_window`` is the
    proxy that marshals calls across to that process.

    Returns:
        The window proxy, or ``None`` when no native window exists.
    """
    try:
        from nicegui import app as nicegui_app

        return nicegui_app.native.main_window
    except (ImportError, AttributeError):  # pragma: no cover - defensive
        return None


async def _native_dialog(
    dialog_type: int,
    directory: Path | None = None,
    allow_multiple: bool = False,
    save_filename: str = "",
    file_types: tuple[str, ...] = (),
) -> tuple[Path, ...] | None:
    """Open an operating-system file dialog.

    Args:
        dialog_type: One of the ``NATIVE_DIALOG_*`` constants.
        directory: Directory to open at.
        allow_multiple: Whether several files may be selected.
        save_filename: Suggested name for a save dialog.
        file_types: pywebview filter strings, e.g. ``"PDF files (*.pdf)"``.

    Returns:
        The chosen paths, an empty tuple when the user cancelled, or ``None``
        when no native dialog is available in this run mode.
    """
    window = native_window()
    if window is None:
        return None

    try:
        selection = await window.create_file_dialog(
            dialog_type,
            directory=str(directory) if directory and directory.is_dir() else "",
            allow_multiple=allow_multiple,
            save_filename=save_filename,
            file_types=file_types,
        )
    except Exception as exc:
        # A dialog failure must degrade to the in-app browser, never crash.
        logger.warning("ui.native_dialog_failed", exc_info=exc)
        return None

    if not selection:
        return ()
    if isinstance(selection, str):
        return (Path(selection),)
    return tuple(Path(str(item)) for item in selection)


def build_file_types(extensions: tuple[str, ...], label: str) -> tuple[str, ...]:
    """Build pywebview file-type filters for the given extensions.

    Args:
        extensions: Dot-prefixed extensions, e.g. ``(".pdf",)``.
        label: Human-readable group name, e.g. ``"PDF documents"``.

    Returns:
        Filter strings, always ending with an "all files" entry.
    """
    if not extensions:
        return (FILE_TYPE_ALL,)
    patterns = ";".join(f"*{ext}" for ext in extensions)
    return (f"{label} ({patterns})", FILE_TYPE_ALL)


async def choose_directory(start: Path | None = None) -> Path | None:
    """Let the user pick a directory, preferring the operating system's dialog.

    Args:
        start: Directory to open at (rule E-03 — callers pass the module's
            last-used directory).

    Returns:
        The selected directory, or ``None`` if the user cancelled.
    """
    native = await _native_dialog(NATIVE_DIALOG_FOLDER, directory=start)
    if native is not None:
        return native[0] if native else None
    return await _browse_directory_dialog(start)


async def choose_files(
    start: Path | None = None,
    extensions: tuple[str, ...] = (),
    label: str = "Supported files",
) -> list[Path]:
    """Let the user pick one or more files, filtered by extension.

    Uses the operating system's dialog in the native window, and an in-app
    browser applying the same filter in a browser tab.

    Args:
        start: Directory to open at.
        extensions: Dot-prefixed extensions to offer, e.g. ``(".pdf",)``.
        label: Group name shown in the dialog's filter.

    Returns:
        The selected files; empty when the user cancelled.
    """
    native = await _native_dialog(
        NATIVE_DIALOG_OPEN,
        directory=start,
        allow_multiple=True,
        file_types=build_file_types(extensions, label),
    )
    if native is not None:
        return list(native)
    return await _browse_files_dialog(start, extensions)


async def choose_save_path(
    suggested_name: str, start: Path | None = None
) -> Path | None:
    """Ask the user where to save a file.

    Args:
        suggested_name: Default filename offered in the dialog.
        start: Directory to open at.

    Returns:
        The chosen destination, or ``None`` when cancelled or unavailable.
    """
    native = await _native_dialog(
        NATIVE_DIALOG_SAVE, directory=start, save_filename=suggested_name
    )
    if native is None:
        return None
    return native[0] if native else None


def reveal_in_file_manager(path: Path) -> bool:
    """Open the platform's file manager showing *path*.

    Args:
        path: File or directory to reveal.

    Returns:
        True when a file manager was launched.
    """
    target = path if path.is_dir() else path.parent
    try:
        if is_windows():
            subprocess.Popen([WINDOWS_FILE_MANAGER, str(target)])
        elif is_macos():
            subprocess.Popen([MACOS_FILE_MANAGER, str(target)])
        else:
            subprocess.Popen([LINUX_FILE_MANAGER, str(target)])
    except OSError as exc:
        logger.warning("ui.reveal_failed — path=%s reason=%s", target, exc)
        return False
    return True


def open_in_default_app(path: Path) -> bool:
    """Open *path* in the operating system's default application for its type.

    This is the "double-click" behaviour — a merged PDF opens in the default
    PDF viewer, a converted .docx in the word processor — on any OS. Nothing
    leaves the machine: it is the same handler the file manager would invoke
    (rule C-01).

    Args:
        path: The file to open.

    Returns:
        True when the open command was launched.
    """
    try:
        if is_windows():
            os.startfile(str(path))  # type: ignore[attr-defined,unused-ignore]
        elif is_macos():
            subprocess.Popen([MACOS_FILE_MANAGER, str(path)])
        else:
            subprocess.Popen([LINUX_FILE_MANAGER, str(path)])
    except OSError as exc:
        logger.warning("ui.open_failed — path=%s reason=%s", path, exc)
        return False
    return True


async def _browse_directory_dialog(start: Path | None) -> Path | None:
    """In-app directory browser used when no native dialog is available.

    Args:
        start: Directory to open at.

    Returns:
        The selected directory, or ``None`` if the user cancelled.
    """
    current = start if start and start.is_dir() else Path.cwd()

    with ui.dialog() as dialog, ui.card().props("flat").style(
        f"background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER};"
        f" border-radius: 12px; padding: 0;"
        f" width: {DIRECTORY_PICKER_WIDTH_PX}px; max-width: 94vw;"
    ):
        with ui.column().style(
            f"gap: 0; width: 100%; padding: 16px 20px;"
            f" border-bottom: 1px solid {COLOR_BORDER};"
        ):
            ui.label("Select a folder").style(
                f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: 700;"
            )
            path_input = (
                ui.input(value=str(current))
                .props("dense outlined")
                .style(
                    f"width: 100%; margin-top: 10px; font-family: monospace;"
                    f" font-size: 12px; color: {COLOR_TEXT_PRIMARY};"
                )
            )
            ui.label("Type a path and press Enter, or browse below.").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 11px; margin-top: 4px;"
            )

        listing = ui.column().style(
            f"gap: 0; width: 100%; padding: 6px;"
            f" height: {DIRECTORY_PICKER_LIST_HEIGHT_PX}px; overflow-y: auto;"
        )

        with ui.row().style(
            f"justify-content: space-between; align-items: center; width: 100%;"
            f" padding: 12px 20px; border-top: 1px solid {COLOR_BORDER};"
        ):
            drives_row = ui.row().style("gap: 4px; flex-wrap: wrap;")
            with ui.row().style("gap: 8px;"):
                ui.button("Cancel", on_click=dialog.close).props(
                    "flat no-caps"
                ).style(f"color: {COLOR_TEXT_MUTED};")
                ui.button(
                    "Select Folder", on_click=lambda: dialog.submit(current)
                ).props("no-caps").style(
                    f"background: {COLOR_PRIMARY}; color: white;"
                    " font-weight: 600; border-radius: 6px;"
                )

    def navigate(target: Path) -> None:
        """Move the browser to *target* and repaint."""
        nonlocal current
        current = target
        path_input.set_value(str(target))
        render()

    def on_path_submitted() -> None:
        """Jump to the typed path, or report that it is not a directory."""
        typed = Path(path_input.value.strip())
        if typed.is_dir():
            navigate(typed)
        else:
            ui.notify(f"Not a directory: {typed}", type="warning")

    def render() -> None:
        """Repaint the drive buttons and the subdirectory list."""
        drives_row.clear()
        with drives_row:
            for root in drive_roots():
                is_active = current.anchor == root.anchor
                ui.button(
                    str(root), on_click=lambda _e, r=root: navigate(r)
                ).props("flat dense no-caps").style(
                    f"color: {COLOR_PRIMARY if is_active else COLOR_TEXT_MUTED};"
                    " font-size: 11px; font-family: monospace;"
                )

        listing.clear()
        with listing:
            parent = current.parent
            if parent != current:
                _picker_row("arrow_upward", "..", lambda: navigate(parent))

            children = subdirectories(current)
            if not children:
                ui.label("No subfolders here.").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 14px;"
                )
                return
            for child in children:
                # partial binds the current child; a bare lambda would close
                # over the loop variable and every row would open the last one.
                _picker_row("folder", child.name, partial(navigate, child))

    path_input.on("keydown.enter", on_path_submitted)
    render()

    chosen = await show_dialog(dialog)
    if chosen is None:
        return None
    logger.debug("ui.directory_chosen — path=%s", chosen)
    return Path(chosen)


def matching_files(path: Path, extensions: tuple[str, ...]) -> list[Path]:
    """List files directly inside *path* matching *extensions*.

    Args:
        path: Directory to list.
        extensions: Dot-prefixed extensions; empty accepts every file.

    Returns:
        Matching files sorted by name, capped at
        :data:`~shared.constants.DIRECTORY_PICKER_MAX_ENTRIES`.
    """
    wanted = {ext.lower() for ext in extensions}
    found: list[Path] = []
    try:
        for entry in path.iterdir():
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if wanted and entry.suffix.lower() not in wanted:
                continue
            found.append(entry)
            if len(found) >= DIRECTORY_PICKER_MAX_ENTRIES:
                break
    except OSError as exc:
        logger.debug("ui.file_listing_failed — path=%s reason=%s", path, exc)
        return []
    found.sort(key=lambda item: item.name.lower())
    return found


async def _browse_files_dialog(
    start: Path | None, extensions: tuple[str, ...]
) -> list[Path]:
    """In-app file browser used when no native dialog is available.

    Args:
        start: Directory to open at.
        extensions: Extensions to show; empty shows every file.

    Returns:
        The selected files; empty when cancelled.
    """
    current = start if start and start.is_dir() else Path.cwd()
    selected: set[Path] = set()

    with ui.dialog() as dialog, ui.card().props("flat").style(
        f"background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER};"
        f" border-radius: 12px; padding: 0;"
        f" width: {DIRECTORY_PICKER_WIDTH_PX}px; max-width: 94vw;"
    ):
        with ui.column().style(
            f"gap: 0; width: 100%; padding: 16px 20px;"
            f" border-bottom: 1px solid {COLOR_BORDER};"
        ):
            ui.label("Select files").style(
                f"color: {COLOR_TEXT_PRIMARY}; font-size: 16px; font-weight: 700;"
            )
            path_input = (
                ui.input(value=str(current))
                .props("dense outlined")
                .style(
                    f"width: 100%; margin-top: 10px; font-family: monospace;"
                    f" font-size: 12px; color: {COLOR_TEXT_PRIMARY};"
                )
            )
            filter_label = ", ".join(extensions) if extensions else "all files"
            ui.label(f"Showing: {filter_label}").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 11px; margin-top: 4px;"
            )

        listing = ui.column().style(
            f"gap: 0; width: 100%; padding: 6px;"
            f" height: {DIRECTORY_PICKER_LIST_HEIGHT_PX}px; overflow-y: auto;"
        )

        with ui.row().style(
            f"justify-content: space-between; align-items: center; width: 100%;"
            f" padding: 12px 20px; border-top: 1px solid {COLOR_BORDER};"
        ):
            count_label = ui.label("0 selected").style(
                f"color: {COLOR_TEXT_DIM}; font-size: 11px;"
            )
            with ui.row().style("gap: 8px;"):
                ui.button("Cancel", on_click=dialog.close).props(
                    "flat no-caps"
                ).style(f"color: {COLOR_TEXT_MUTED};")
                ui.button(
                    "Add Selected", on_click=lambda: dialog.submit(sorted(selected))
                ).props("no-caps").style(
                    f"background: {COLOR_PRIMARY}; color: white;"
                    " font-weight: 600; border-radius: 6px;"
                )

    def update_count() -> None:
        """Refresh the selection counter."""
        count_label.set_text(f"{len(selected)} selected")

    def toggle(path: Path, checked: bool) -> None:
        """Add or remove a file from the selection."""
        if checked:
            selected.add(path)
        else:
            selected.discard(path)
        update_count()

    def navigate(target: Path) -> None:
        """Move to *target* and repaint."""
        nonlocal current
        current = target
        path_input.set_value(str(target))
        render()

    def on_path_submitted() -> None:
        """Jump to the typed path."""
        typed = Path(path_input.value.strip())
        if typed.is_dir():
            navigate(typed)
        else:
            ui.notify(f"Not a directory: {typed}", type="warning")

    def render() -> None:
        """Repaint the folder and file rows."""
        listing.clear()
        with listing:
            parent = current.parent
            if parent != current:
                _picker_row("arrow_upward", "..", partial(navigate, parent))
            for child in subdirectories(current):
                _picker_row("folder", child.name, partial(navigate, child))

            files = matching_files(current, extensions)
            if not files:
                ui.label("No matching files in this folder.").style(
                    f"color: {COLOR_TEXT_DIM}; font-size: 12px; padding: 14px;"
                )
                return
            for file_path in files:
                _file_row(file_path, file_path in selected, toggle)

    path_input.on("keydown.enter", on_path_submitted)
    render()
    update_count()

    chosen = await show_dialog(dialog)
    return [Path(p) for p in chosen] if chosen else []


def _file_row(
    path: Path, checked: bool, on_toggle: Callable[[Path, bool], None]
) -> None:
    """Render one selectable file row.

    Args:
        path: The file.
        checked: Whether it is currently selected.
        on_toggle: Called with the file and its new checked state.
    """
    with ui.row().style(
        "align-items: center; gap: 10px; width: 100%; padding: 5px 14px;"
    ):
        ui.checkbox(
            value=checked, on_change=lambda e: on_toggle(path, bool(e.value))
        ).props("dense")
        ui.icon("description").style(f"color: {COLOR_TEXT_DIM}; font-size: 15px;")
        ui.label(path.name).style(
            f"color: {COLOR_TEXT_MUTED}; font-size: 13px; flex: 1;"
            " word-break: break-all;"
        )
        ui.label(format_bytes(_safe_size(path))).style(
            f"color: {COLOR_TEXT_DIM}; font-size: 11px;"
        )


def _safe_size(path: Path) -> int:
    """Return a file's size, or 0 when it cannot be read.

    One unreadable entry — a file deleted mid-browse, or a permission-denied
    special file — must not abort the whole picker's render.

    Args:
        path: The file to measure.

    Returns:
        Size in bytes, or 0 when unavailable.
    """
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError as exc:
        logger.debug("ui.size_probe_failed — path=%s reason=%s", path, exc)
        return 0


def _picker_row(icon: str, label: str, on_click: Callable[[], None]) -> None:
    """Render one clickable row inside the directory picker.

    Args:
        icon: Material icon name.
        label: Row text.
        on_click: Invoked when the row is clicked.
    """
    with ui.row().style(
        "align-items: center; gap: 10px; width: 100%; padding: 7px 14px;"
        " border-radius: 6px; cursor: pointer;"
    ).on("click", lambda _e: on_click()):
        ui.icon(icon).style(f"color: {COLOR_TEXT_DIM}; font-size: 16px;")
        ui.label(label).style(f"color: {COLOR_TEXT_MUTED}; font-size: 13px;")


def is_probably_binary(path: Path, sample_bytes: int = BINARY_SNIFF_BYTES) -> bool:
    """Return True when *path* does not look like text.

    A NUL byte in the first few kilobytes is the same heuristic ``git`` and
    ``file`` use, and it is decisive in practice: no real text encoding this app
    will meet emits one, and every common binary container does.

    Args:
        path: The file to sniff.
        sample_bytes: How much of the head to inspect.

    Returns:
        True when the file appears to be binary, and False when it looks like
        text or cannot be read (in which case the reader reports the real
        error).
    """
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(sample_bytes)
    except OSError as exc:
        logger.debug("ui.sniff_failed — path=%s reason=%s", path, exc)
        return False


def read_text_preview(path: Path, max_chars: int = FILE_PREVIEW_MAX_CHARS) -> str:
    """Read the head of a text file for on-screen preview.

    A binary file is reported as such rather than rendered. Decoding a PDF or a
    video as UTF-8 with ``errors="replace"`` produced a screenful of replacement
    characters that looked like corruption — the file is fine, it simply is not
    text, and saying so is more useful than showing the mojibake.

    Large exports are truncated so the browser is never asked to render a
    multi-megabyte string.

    Args:
        path: File to read.
        max_chars: Maximum characters to return.

    Returns:
        The file's leading content, with a truncation notice appended when it
        was cut short, or an error line when the file cannot be read.
    """
    if is_probably_binary(path):
        return BINARY_PREVIEW_NOTICE.format(name=path.name)

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(max_chars + 1)
    except OSError as exc:
        logger.warning("ui.preview_failed — path=%s reason=%s", path, exc)
        return f"Could not read the file: {exc}"

    if len(content) > max_chars:
        return content[:max_chars] + FILE_PREVIEW_TRUNCATION_NOTICE
    return content


def output_file_actions(path: Path) -> None:
    """Render the standard actions for a file a module has just produced.

    Leads with **Open** — launching the file in the OS default app (a merged
    PDF in the PDF viewer, etc.) is the thing a user most reliably wants next —
    followed by in-app view, copy, reveal, and save.

    Args:
        path: The produced file.
    """
    with ui.row().style(
        f"align-items: center; gap: 8px; margin-top: 14px; width: 100%;"
        f" background: {COLOR_OVERLAY}; border-radius: 6px; padding: 10px 14px;"
        " flex-wrap: wrap;"
    ):
        ui.label(str(path)).style(
            f"color: {COLOR_ACCENT}; font-size: 12px; font-family: monospace;"
            " flex: 1; min-width: 200px; word-break: break-all;"
        )
        ui.button(
            "Open", icon="open_in_new", on_click=lambda: _open_default(path)
        ).props("no-caps dense").style(
            f"background: {COLOR_PRIMARY}; color: white;"
            " border-radius: 6px; font-weight: 600;"
        ).tooltip("Open in your system's default app")
        ui.button("View", on_click=partial(_show_file_preview, path)).props(
            "flat dense no-caps"
        ).style(f"color: {COLOR_TEXT_MUTED};")
        ui.button(
            "Copy Path", on_click=lambda: copy_to_clipboard(str(path))
        ).props("flat dense no-caps").style(f"color: {COLOR_TEXT_MUTED};")
        ui.button(
            "Copy Content", on_click=lambda: _copy_file_content(path)
        ).props("flat dense no-caps").style(f"color: {COLOR_TEXT_MUTED};")
        ui.button(
            "Open Folder", on_click=lambda: _reveal(path)
        ).props("flat dense no-caps").style(f"color: {COLOR_TEXT_MUTED};")
        ui.button("Save As…", on_click=lambda: _save_file_as(path)).props(
            "flat dense no-caps"
        ).style(f"color: {COLOR_TEXT_MUTED};")


def _open_default(path: Path) -> None:
    """Open a produced file in the OS default app, reporting failure clearly.

    Args:
        path: File to open.
    """
    if not path.exists():
        ui.notify(f"File no longer exists: {path}", type="negative")
        return
    if not open_in_default_app(path):
        ui.notify("Could not open the file in its default app.", type="negative")


def pagination_controls(
    page: int,
    total_items: int,
    page_size: int,
    on_page: Callable[[int], None],
    *,
    noun: str = "items",
) -> None:
    """Render Prev / "Page X of Y · N noun" / Next below a paged list.

    Keeps the rendered DOM bounded to a single page (so a huge result set stays
    responsive) while still letting the user reach every item — unlike a hard
    cap that silently hides the remainder. Renders nothing when everything fits
    on one page.

    Args:
        page: Current 0-based page index (clamped to a valid range).
        total_items: Total number of items across all pages.
        page_size: Items shown per page (must be > 0).
        on_page: Called with the new 0-based page index when the user pages.
        noun: Plural noun for the count label, e.g. ``"groups"``.
    """
    pages = max(1, (total_items + page_size - 1) // page_size)
    if pages <= 1:
        return
    page = max(0, min(page, pages - 1))

    with ui.row().style(
        "align-items: center; justify-content: center; gap: 14px;"
        " width: 100%; margin-top: 10px;"
    ):
        prev_button = ui.button(
            icon="chevron_left", on_click=lambda: on_page(page - 1)
        ).props("flat dense round").style(f"color: {COLOR_TEXT_MUTED};")
        if page == 0:
            prev_button.disable()
        ui.label(f"Page {page + 1} of {pages} · {total_items:,} {noun}").style(
            f"color: {COLOR_TEXT_DIM}; font-size: 12px;"
        )
        next_button = ui.button(
            icon="chevron_right", on_click=lambda: on_page(page + 1)
        ).props("flat dense round").style(f"color: {COLOR_TEXT_MUTED};")
        if page >= pages - 1:
            next_button.disable()


def _reveal(path: Path) -> None:
    """Open the file manager at *path*, reporting failure to the user.

    Args:
        path: File to reveal.
    """
    if not path.exists():
        ui.notify(f"File no longer exists: {path}", type="negative")
        return
    if not reveal_in_file_manager(path):
        ui.notify("Could not open the file manager.", type="negative")


async def _save_file_as(path: Path) -> None:
    """Copy a produced file to a location the user chooses.

    In the native window this opens the operating system's Save dialog. In a
    browser tab it falls back to a normal download, which is the only thing a
    page is permitted to do.

    Args:
        path: The file to deliver.
    """
    if not path.is_file():
        ui.notify(f"File no longer exists: {path}", type="negative")
        return

    if native_window() is None:
        ui.download(path)
        return

    destination = await choose_save_path(path.name, path.parent)
    if destination is None:
        return

    try:
        if destination.resolve() == path.resolve():
            ui.notify("That is the file's current location.", type="info")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    except OSError as exc:
        logger.error("ui.save_as_failed — to=%s", destination, exc_info=exc)
        ui.notify(f"Could not save the file: {exc}", type="negative")
        return

    ui.notify(f"Saved to {destination}", type="positive")


def _copy_file_content(path: Path) -> None:
    """Copy a file's contents to the clipboard, up to a size ceiling.

    An export can be hundreds of megabytes. Reading one whole into memory and
    then into a JavaScript string literal would stall the event loop and can
    exhaust the browser — so the read is capped the same way the on-screen
    preview is, and the user is told when it was cut short.

    Args:
        path: File to read.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(CLIPBOARD_MAX_CHARS + 1)
    except OSError as exc:
        logger.error("ui.copy_content_failed — path=%s", path, exc_info=exc)
        ui.notify(f"Could not read the file: {exc}", type="negative")
        return

    truncated = len(content) > CLIPBOARD_MAX_CHARS
    copy_to_clipboard(content[:CLIPBOARD_MAX_CHARS])
    if truncated:
        ui.notify(
            f"Copied the first {CLIPBOARD_MAX_CHARS:,} characters — the file is "
            "too large to copy whole. Use Save As… for the full contents.",
            type="warning",
            timeout=8000,
        )
    else:
        ui.notify("Content copied to clipboard.", type="positive")


async def _show_file_preview(path: Path) -> None:
    """Open a scrollable viewer showing the file's content.

    Async so the dialog can be awaited and then disposed of — it holds up to
    :data:`~shared.constants.FILE_PREVIEW_MAX_CHARS` of text, so leaving one
    behind per click is the most expensive of the dialog leaks.

    Args:
        path: File to display.
    """
    content = read_text_preview(path)
    with ui.dialog() as dialog, ui.card().props("flat").style(
        f"background: {COLOR_CARD_BG}; border: 1px solid {COLOR_BORDER};"
        f" border-radius: 12px; padding: 0;"
        f" width: {FILE_PREVIEW_WIDTH_PX}px; max-width: 94vw;"
    ):
        with ui.row().style(
            f"align-items: center; justify-content: space-between; width: 100%;"
            f" padding: 14px 20px; border-bottom: 1px solid {COLOR_BORDER};"
        ):
            ui.label(path.name).style(
                f"color: {COLOR_TEXT_PRIMARY}; font-size: 14px; font-weight: 600;"
                " font-family: monospace;"
            )
            with ui.row().style("gap: 6px;"):
                ui.button(
                    "Copy Content", on_click=lambda: _copy_file_content(path)
                ).props("flat dense no-caps").style(f"color: {COLOR_TEXT_MUTED};")
                ui.button("Close", on_click=dialog.close).props(
                    "flat dense no-caps"
                ).style(f"color: {COLOR_TEXT_MUTED};")

        ui.label(content).style(
            f"color: {COLOR_TEXT_MUTED}; font-size: 11px; font-family: monospace;"
            f" white-space: pre-wrap; word-break: break-word; padding: 16px 20px;"
            f" height: {FILE_PREVIEW_HEIGHT_PX}px; overflow-y: auto; width: 100%;"
        )

    dialog.open()
    await show_dialog(dialog)


def stat_chip(label: str, value: str, colour: str = COLOR_PRIMARY) -> None:
    """Render a small labelled statistic tile.

    Args:
        label: Descriptive caption shown beneath the value.
        value: The statistic to display.
        colour: CSS colour for the value text.
    """
    with ui.card().props("flat").style(
        f"background: {COLOR_OVERLAY}; border: 1px solid {COLOR_BORDER};"
        " border-radius: 8px; padding: 12px 16px; text-align: center;"
    ):
        ui.label(value).style(f"color: {colour}; font-size: 22px; font-weight: 700;")
        ui.label(label).style(f"color: {COLOR_TEXT_DIM}; font-size: 11px;")
