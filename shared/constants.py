"""Application-wide constants for OmniForge.

No magic numbers anywhere else in the codebase (rule D-05).
All literal values that appear in more than one place live here or in a
module-specific ``constants.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _resolve_app_root() -> Path:
    """Locate the writable application root, independent of the working dir.

    In a normal source checkout this is the repository root (the parent of
    ``shared/``). Under a PyInstaller freeze ``__file__`` points inside the
    read-only ``_MEI`` bundle, so the writable root is the directory next to
    the executable instead.

    Anchoring every write location here means ``exports/``, ``temp/``,
    ``data/``, the log directory, the recycle store and the single-instance
    lock resolve to the same place no matter where the app is launched from —
    a Start-menu shortcut (CWD ``C:\\Windows\\System32``), a service, or
    ``python app.py`` invoked from another directory. Deriving them from the
    current working directory instead silently scattered outputs and the lock
    across whatever folder happened to be current.

    Returns:
        Absolute path to the application root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# ─── Application Identity ──────────────────────────────────────────────────────
APP_TITLE: str = "OmniForge"
# Single source of truth for the version — app.py had it hard-coded in its
# startup log, which is exactly the duplication rule D-05 exists to prevent.
APP_VERSION: str = "1.0.0"
APP_HOST: str = "127.0.0.1"
APP_PORT: int = 8765

# ─── Version Tiers (RFC 0007) ──────────────────────────────────────────────────
# OmniForge ships as a versioned product. Every module manifest declares a
# "tier"; the registry only loads modules whose tier is in SHIPPED_TIERS, so a
# built-but-parked tier never appears in the UI or pulls its dependencies.
# V1 = "Offline Developer File Toolkit" — Converters + Extractors.
# V2 = System (system_matrix), V3 = Security (network_vault): built, parked.
VALID_TIERS: frozenset[str] = frozenset({"v1", "v2", "v3"})
#: The tier(s) the current build ships. Promoting a tier is a one-line change
#: here plus moving its modules back under ``modules/``.
SHIPPED_TIERS: frozenset[str] = frozenset({"v1"})

# ─── File System ───────────────────────────────────────────────────────────────
# Absolute, CWD-independent (see :func:`_resolve_app_root`). Stored as strings
# so the many ``Path(EXPORTS_DIR)`` call sites keep working unchanged.
APP_ROOT: Path = _resolve_app_root()
LOCK_FILE_PATH: str = str(APP_ROOT / "data" / "omniforge.lock")
# A lock file is created and then written, so a competing launch can catch it
# momentarily empty. Re-reading a few times tells "still being written" apart
# from "abandoned by a crash" without needing a timestamp (RFC 0005).
LOCK_READ_ATTEMPTS: int = 5
LOCK_READ_RETRY_SECONDS: float = 0.05
EXPORTS_DIR: str = str(APP_ROOT / "exports")
TEMP_DIR: str = str(APP_ROOT / "temp")
DATA_DIR: str = str(APP_ROOT / "data")

# ─── Storage ───────────────────────────────────────────────────────────────────
# How long the writer waits after a change before writing, so a burst of
# updates (a path typed into an input, one event per keystroke) collapses into
# a single database write instead of one per character.
STORAGE_FLUSH_DEBOUNCE_SECONDS: float = 0.4
# Ceiling on how long a shutdown flush waits for the writer to finish.
STORAGE_FLUSH_JOIN_SECONDS: float = 5.0

# ─── Logging ───────────────────────────────────────────────────────────────────
LOG_ROOT_NAME: str = "omniforge"
LOG_DIR: str = str(APP_ROOT / "data" / "logs")
# Static, NOT date-stamped. TimedRotatingFileHandler owns the dating: the live
# file is always omniforge.log and a midnight rollover becomes
# omniforge.log.2026-07-24. Putting a date in the name as well made the two
# mechanisms fight — the handler kept writing to a base file named for the day
# the *session started*, so a run spanning midnight filed day two's records
# under day one and the retention window counted the wrong thing (RFC 0005).
LOG_FILENAME: str = "omniforge.log"
LOG_ROTATION_WHEN: str = "midnight"
LOG_RETENTION_DAYS: int = 14

# Optional structured fields lifted from a LogRecord into the JSON log line
# when supplied via ``logger.info(..., extra={...})``.
LOG_STRUCTURED_FIELDS: tuple[str, ...] = (
    "module_id",
    "operation",
    "duration_ms",
    "status",
)

# ─── Sandbox / Execution ───────────────────────────────────────────────────────
DEFAULT_EXECUTION_TIMEOUT_SECONDS: int = 300
WATCHDOG_STALL_SECONDS: int = 30
# How long a progress drain waits on its queue before re-checking whether the
# work has finished. Short enough to feel live, long enough not to spin.
PROGRESS_QUEUE_POLL_SECONDS: float = 0.2

# ─── Platform ──────────────────────────────────────────────────────────────────
# sys.platform prefixes, matched by shared.platform_info.
PLATFORM_WINDOWS: str = "win32"
PLATFORM_LINUX: str = "linux"
PLATFORM_DARWIN: str = "darwin"

# Windows 11 kept the 10.0 kernel version, so only the build number tells the
# two releases apart. platform.release() reports "10" on both.
WINDOWS_11_MIN_BUILD: int = 22000

OS_DISPLAY_NAMES: dict[str, str] = {
    "windows": "Windows",
    "linux": "Linux",
    "macos": "macOS",
    "unknown": "Unknown OS",
}

# Material icon names for the host badge in the shell header.
OS_ICONS: dict[str, str] = {
    "windows": "desktop_windows",
    "linux": "terminal",
    "macos": "laptop_mac",
    "unknown": "computer",
}

# ─── Filename Safety ───────────────────────────────────────────────────────────
# Characters Windows forbids in filenames (POSIX allows most, but generated
# names must stay portable across all three target platforms).
RESERVED_FILENAME_CHARS: frozenset[str] = frozenset('<>:"|?*\0')

# Legacy MS-DOS device names that cannot be used as filenames on Windows.
WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# ─── File Preview ──────────────────────────────────────────────────────────────
FILE_PREVIEW_WIDTH_PX: int = 900
FILE_PREVIEW_HEIGHT_PX: int = 480
# Exports can be many megabytes; the browser is only handed the head of one.
FILE_PREVIEW_MAX_CHARS: int = 200_000
FILE_PREVIEW_TRUNCATION_NOTICE: str = (
    "\n\n… preview truncated — download or open the file to see the rest."
)
# Ceiling on a "Copy Content" action. The text has to cross into a JavaScript
# string literal, so an uncapped multi-megabyte export would stall the event
# loop and can exhaust the browser tab.
CLIPBOARD_MAX_CHARS: int = 200_000
# How much of a file's head is sniffed to decide whether it is text. A NUL byte
# in this window means binary — the same test git and file(1) use.
BINARY_SNIFF_BYTES: int = 8192
BINARY_PREVIEW_NOTICE: str = (
    "{name} is a binary file, so there is nothing readable to show here.\n\n"
    "Use Open to view it in the application that understands it."
)

# ─── Native File Dialogs ───────────────────────────────────────────────────────
# pywebview's FileDialog enum values. Hard-coded so the constants resolve even
# when pywebview is absent (browser-only installs); they are a stable part of
# its public API.
NATIVE_DIALOG_OPEN: int = 10
NATIVE_DIALOG_FOLDER: int = 20
NATIVE_DIALOG_SAVE: int = 30

# Shown as the last entry of every native file-type filter.
FILE_TYPE_ALL: str = "All files (*.*)"

# Commands that open the platform's file manager at a given path.
WINDOWS_FILE_MANAGER: str = "explorer"
MACOS_FILE_MANAGER: str = "open"
LINUX_FILE_MANAGER: str = "xdg-open"

# ─── Directory Picker ──────────────────────────────────────────────────────────
DIRECTORY_PICKER_WIDTH_PX: int = 620
DIRECTORY_PICKER_LIST_HEIGHT_PX: int = 320
# Guards the dialog against stalling on directories with huge child counts.
DIRECTORY_PICKER_MAX_ENTRIES: int = 500

# ─── Command Palette (rule E-06) ───────────────────────────────────────────────
# Name of the custom browser event the Ctrl+K listener emits. The chord is
# matched client-side so ordinary typing never crosses the websocket.
PALETTE_OPEN_EVENT: str = "omniforge_palette_open"
PALETTE_RESULT_LIMIT: int = 8
RECENT_OPERATIONS_LIMIT: int = 20
STORAGE_KEY_RECENT_OPERATIONS: str = "recent_operations"

# Fuzzy-match scoring weights. Higher is a better match.
FUZZY_CHARACTER_SCORE: int = 1
FUZZY_CONSECUTIVE_BONUS: int = 8
FUZZY_WORD_BOUNDARY_BONUS: int = 10
FUZZY_PREFIX_BONUS: int = 25
FUZZY_SUBSTRING_BONUS: int = 15
# Subtracted per character skipped before a match, so earlier hits rank higher.
FUZZY_GAP_PENALTY: int = 1
# Characters treated as word separators when awarding the boundary bonus.
FUZZY_WORD_SEPARATORS: frozenset[str] = frozenset(" _-./")

# Field weights — a hit in the title outranks the same hit in a tag.
FUZZY_TITLE_WEIGHT: int = 3
FUZZY_KEYWORD_WEIGHT: int = 2
FUZZY_SUBTITLE_WEIGHT: int = 1

# ─── Privilege Elevation (rule B-05) ───────────────────────────────────────────
# Seconds to wait for an elevated command before giving up.
ELEVATION_TIMEOUT_SECONDS: int = 120

# POSIX escalation helpers, in preference order. pkexec shows a polkit GUI
# prompt; sudo is the fallback where polkit is unavailable.
POSIX_ESCALATION_BINARIES: tuple[str, ...] = ("pkexec", "sudo")

# Win32 ShellExecuteEx constants.
SW_HIDE: int = 0
SEE_MASK_NOCLOSEPROCESS: int = 0x00000040
SEE_MASK_NO_CONSOLE: int = 0x00008000
# ShellExecuteEx sets this when the user dismisses the UAC prompt.
ERROR_CANCELLED: int = 1223
# WaitForSingleObject return value when the wait elapsed.
WAIT_TIMEOUT: int = 0x00000102
# Sentinel returned by WaitForSingleObject on failure.
WAIT_FAILED: int = 0xFFFFFFFF

# ─── Process Safety ────────────────────────────────────────────────────────────
# PIDs the operating system reserves and that must never be terminated.
# Windows: 0 is System Idle, 4 is the System process. POSIX: 1 is init.
WINDOWS_CRITICAL_PIDS: frozenset[int] = frozenset({0, 4})
POSIX_INIT_PID: int = 1

# ─── Recycle Store (rule B-04) ─────────────────────────────────────────────────
RECYCLE_DIR: str = str(APP_ROOT / "data" / "recycle")
# Payload directory used when a source lives on a different volume from the app.
# Moving across volumes turns a rename into a full copy — multi-GB and slow — so
# the payload is written to the source's own volume instead. See RFC 0003.
RECYCLE_LOCAL_DIRNAME: str = ".omniforge-recycle"
RECYCLE_MANIFEST_FILENAME: str = "manifest.json"
RECYCLE_PAYLOAD_DIRNAME: str = "files"
RECYCLE_RETENTION_HOURS: int = 24
# Microseconds keep batch IDs unique when several run in the same second.
RECYCLE_BATCH_ID_FORMAT: str = "%Y%m%d_%H%M%S_%f"

# ─── Hashing ───────────────────────────────────────────────────────────────────
# Read size for streamed file hashing — large enough to keep xxhash fed,
# small enough that multi-GB files never load into memory.
HASH_CHUNK_SIZE_BYTES: int = 1024 * 1024

# ─── Formatting ────────────────────────────────────────────────────────────────
# Sizes use binary multiples, matching desktop file managers.
BYTES_PER_UNIT: int = 1024
SIZE_UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB", "PB")
SECONDS_PER_MINUTE: int = 60
SECONDS_PER_HOUR: int = 3600
MILLISECONDS_PER_SECOND: int = 1000

# ─── Win32 ─────────────────────────────────────────────────────────────────────
# MB_ICONERROR flag for user32.MessageBoxW.
MB_ICONERROR: int = 0x10

# ─── UI Layout ─────────────────────────────────────────────────────────────────
SIDEBAR_WIDTH_PX: int = 220
HEADER_HEIGHT_PX: int = 56
WINDOW_WIDTH: int = 1280
WINDOW_HEIGHT: int = 800

# ─── Theme ─────────────────────────────────────────────────────────────────────
# The id of the <style> element holding the active palette. Rewriting its
# contents is what makes a theme switch instant (rule E-07).
THEME_STYLE_ELEMENT_ID: str = "omniforge-theme"

# App-level storage, shared by settings that are not owned by any one module.
STORAGE_TABLE_APP: str = "app"
STORAGE_KEY_THEME: str = "theme"

# ─── Colours ───────────────────────────────────────────────────────────────────
# These are CSS custom-property references, not literals. The concrete values
# for each theme live in ``core.theme_engine.PALETTES`` and are injected into
# :root at page load, so a theme change needs no restart and no UI rebuild.
COLOR_PRIMARY: str = "var(--of-primary)"
COLOR_SECONDARY: str = "var(--of-secondary)"
COLOR_ACCENT: str = "var(--of-accent)"
COLOR_POSITIVE: str = "var(--of-positive)"
COLOR_NEGATIVE: str = "var(--of-negative)"
COLOR_WARNING: str = "var(--of-warning)"
COLOR_DARK_BG: str = "var(--of-bg)"
COLOR_SIDEBAR_BG: str = "var(--of-surface)"
COLOR_CARD_BG: str = "var(--of-surface)"
COLOR_BORDER: str = "var(--of-border)"
COLOR_TEXT_PRIMARY: str = "var(--of-text-primary)"
COLOR_TEXT_MUTED: str = "var(--of-text-muted)"
COLOR_TEXT_DIM: str = "var(--of-text-dim)"
# Translucent fills used for hover/active states and inset panels.
COLOR_PRIMARY_SOFT: str = "var(--of-primary-soft)"
COLOR_NEGATIVE_SOFT: str = "var(--of-negative-soft)"
COLOR_OVERLAY: str = "var(--of-overlay)"

# Longest tail of an elevated command's stderr surfaced in a failure message.
# Enough to name the cause, short enough not to flood a notification.
ELEVATION_STDERR_MAX_CHARS: int = 300
