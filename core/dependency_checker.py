"""Startup dependency validation for OmniForge.

Checks platform-specific requirements before the UI is shown.
Currently validates:
- WebView2 Runtime (Windows only, required by ``nicegui native=True``).

Returns human-readable error strings so the caller can display them.
"""
from __future__ import annotations

import importlib.util
import sys

from core.logger import get_logger

logger = get_logger(__name__)

# WebView2 is registered under this GUID in the Windows registry.
_WEBVIEW2_REG_PATH = (
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
    r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)
_WEBVIEW2_DOWNLOAD_URL = (
    "https://developer.microsoft.com/en-us/microsoft-edge/webview2/"
)


def _is_webview2_installed() -> bool:
    """Return True if WebView2 Runtime is registered on this machine.

    Probes HKEY_LOCAL_MACHINE (machine-wide install) and HKEY_CURRENT_USER
    (per-user install) in **both** registry views. EdgeUpdate is a 32-bit
    application, so a machine-wide install records itself under
    ``WOW6432Node``; a 64-bit Python is redirected away from that key unless
    ``KEY_WOW64_32KEY`` is requested explicitly. Checking only the native
    view reports an installed runtime as missing.

    Returns:
        True on non-Windows platforms (not required) or when the runtime
        is installed; False otherwise.
    """
    if sys.platform != "win32":
        return True
    try:
        # winreg exists only on Windows; the unused-ignore keeps mypy quiet
        # both here (where it resolves) and on POSIX (where it does not).
        import winreg  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        logger.warning("dependency.winreg_unavailable")
        return False

    hives = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    views = (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY)
    for hive in hives:
        for view in views:
            try:
                winreg.OpenKey(hive, _WEBVIEW2_REG_PATH, 0, winreg.KEY_READ | view).Close()
                return True
            except OSError:
                continue
    logger.warning("dependency.webview2_missing")
    return False


def _is_pywebview_installed() -> bool:
    """Return True when the ``pywebview`` package is importable.

    NiceGUI's ``native=True`` mode is built on pywebview. Without it the
    window cannot open, so this is checked alongside the runtime rather than
    surfacing as an import error mid-launch.

    Returns:
        True when the package is present.
    """
    return importlib.util.find_spec("webview") is not None


def validate_startup() -> list[str]:
    """Run all startup dependency checks.

    Returns:
        Empty list when all dependencies are satisfied.
        Each string in the returned list is a human-readable error message
        suitable for display in a dialog or stderr.
    """
    errors: list[str] = []

    if not _is_webview2_installed():
        errors.append(
            "WebView2 Runtime is required on Windows for the native window.\n"
            f"Download from: {_WEBVIEW2_DOWNLOAD_URL}\n"
            "Or start in a browser tab instead:  python app.py --browser"
        )

    if not _is_pywebview_installed():
        errors.append(
            "The 'pywebview' package is required for the native window.\n"
            "Install it with:  pip install pywebview\n"
            "Or start in a browser tab instead:  python app.py --browser"
        )

    return errors
