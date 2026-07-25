"""OmniForge — application entry point.

Startup sequence:
1. Validate platform dependencies (WebView2 on Windows).
2. Acquire single-instance file lock (``data/omniforge.lock``).
3. Register async startup/shutdown hooks.
4. Launch NiceGUI native window on port 8765.

The ``@ui.page("/")`` handler discovers all modules via the Registry
and hands them to the Shell renderer.

Flags:
    --browser   Run in browser tab instead of native window (skips WebView2 check).
"""
from __future__ import annotations

import asyncio
import multiprocessing
import sys
from pathlib import Path

from nicegui import app, ui

from core import recycle_store, single_instance, storage
from core.dependency_checker import validate_startup
from core.logger import get_logger
from core.registry import registry
from core.sandbox import shutdown_process_pool
from shared.constants import (
    APP_HOST,
    APP_PORT,
    APP_TITLE,
    APP_VERSION,
    LOCK_FILE_PATH,
    MB_ICONERROR,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
)
from ui.shell import Shell

logger = get_logger(__name__)

_LOCK = Path(LOCK_FILE_PATH)


# ─── NiceGUI page ─────────────────────────────────────────────────────────────


@ui.page("/")
async def index() -> None:
    """Root page rendered by NiceGUI on every client connection.

    Discovers and loads all modules, then renders the application shell.
    """
    await registry.discover_and_load()
    Shell(
        modules=registry.all_modules(),
        degraded=registry.degraded_modules(),
        metadata=registry.all_metadata(),
    ).render()


# ─── Lifecycle hooks ──────────────────────────────────────────────────────────


async def _on_startup() -> None:
    """Async startup hook — runs before any page is served.

    Expired recycle batches are purged here. Without this the store only ever
    grew: a "purge" moved bytes into ``data/recycle`` and nothing ever released
    them, so the 24-hour retention window (rule B-04) never actually expired.
    """
    logger.info("app.startup — version=%s port=%d", APP_VERSION, APP_PORT)
    purged = await asyncio.to_thread(recycle_store.purge_expired)
    if purged:
        logger.info("app.recycle_purged — batches=%d", purged)


async def _on_shutdown() -> None:
    """Async shutdown hook — unloads modules and releases the lock.

    Settings are written back before exit: ``core.storage`` defers writes to a
    background thread so typing a path never stutters the event loop, so a
    clean shutdown has to wait for that queue to drain (RFC 0005).
    """
    logger.info("app.shutdown")
    await registry.unload_all()
    shutdown_process_pool()
    if not await asyncio.to_thread(storage.flush):
        logger.warning("app.storage_flush_timeout — some settings may be unsaved")
    single_instance.release(_LOCK)


# ─── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    """Validate environment, acquire lock, and start the UI.

    Pass ``--browser`` to open in the default browser instead of a native
    window (bypasses WebView2 requirement, useful for debugging or CI).
    """
    browser_mode: bool = "--browser" in sys.argv

    if not browser_mode:
        errors = validate_startup()
        if errors:
            for msg in errors:
                logger.error("app.startup_validation_failed — %s", msg)
            # On Windows, show a simple message box before exiting.
            if sys.platform == "win32":
                try:
                    import ctypes

                    ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined,unused-ignore]
                        0, "\n\n".join(errors), "OmniForge — Missing Dependency", MB_ICONERROR
                    )
                except OSError as exc:
                    # The dialog is a courtesy — the errors are already logged.
                    logger.error("app.messagebox_failed", exc_info=exc)
            sys.exit(1)

    if not single_instance.acquire(_LOCK):
        logger.error("app.single_instance_violation — another OmniForge is running")
        sys.exit(1)

    app.on_startup(_on_startup)
    app.on_shutdown(_on_shutdown)

    if browser_mode:
        logger.info("app.browser_mode — native window disabled")
        ui.run(
            title=APP_TITLE,
            host=APP_HOST,
            port=APP_PORT,
            native=False,
            reload=False,
            show=True,
            dark=True,
            favicon="⚒",
        )
    else:
        # pywebview disables downloads by default, which silently drops every
        # ui.download() in the native window. Nothing here leaves the machine —
        # a "download" is the app handing the user a file it just produced.
        app.native.settings["ALLOW_DOWNLOADS"] = True
        ui.run(
            title=APP_TITLE,
            host=APP_HOST,
            port=APP_PORT,
            native=True,
            window_size=(WINDOW_WIDTH, WINDOW_HEIGHT),
            reload=False,
            show=False,
            dark=True,
            favicon="⚒",
        )


if __name__ == "__main__":
    # Required before any other multiprocessing use on a spawn-based platform
    # (Windows, macOS) once PyInstaller freezes this into a single binary —
    # otherwise each worker process launched by core.sandbox.run_in_process
    # would try to re-execute the whole application. A no-op everywhere else.
    multiprocessing.freeze_support()
    main()
