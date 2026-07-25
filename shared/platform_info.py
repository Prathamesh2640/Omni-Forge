"""Single source of truth for host operating-system facts.

Every module that needs to branch on the platform reads it from here rather
than testing ``sys.platform`` inline, so cross-platform behaviour stays
auditable and testable in one place.

Usage::

    from shared.platform_info import current_os, describe_host, OperatingSystem

    if current_os() is OperatingSystem.WINDOWS:
        ...
"""
from __future__ import annotations

import enum
import platform
import sys

from pydantic import BaseModel

from shared.constants import (
    BYTES_PER_UNIT,
    OS_DISPLAY_NAMES,
    PLATFORM_DARWIN,
    PLATFORM_LINUX,
    PLATFORM_WINDOWS,
    WINDOWS_11_MIN_BUILD,
)


class OperatingSystem(enum.StrEnum):
    """The operating systems OmniForge targets."""

    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    UNKNOWN = "unknown"


class HostInfo(BaseModel):
    """Description of the machine OmniForge is running on.

    Attributes:
        os: The detected operating system family.
        display_name: Human-readable OS name, e.g. ``"Windows"``.
        release: OS release string, e.g. ``"11"`` or ``"6.5.0-generic"``.
        version: Detailed OS version string.
        architecture: Machine architecture, e.g. ``"AMD64"``, ``"arm64"``.
        python_version: Running interpreter version.
        hostname: Network name of the machine.
    """

    os: OperatingSystem
    display_name: str
    release: str
    version: str
    architecture: str
    python_version: str
    hostname: str

    @property
    def short_label(self) -> str:
        """Compact label for the UI, e.g. ``"Windows 11 · AMD64"``."""
        release = f" {self.release}" if self.release else ""
        return f"{self.display_name}{release} · {self.architecture}"


def current_os() -> OperatingSystem:
    """Return the operating-system family this process is running on.

    Returns:
        The matching :class:`OperatingSystem`, or ``UNKNOWN`` on an
        unrecognised platform (BSD, Android, and so on).
    """
    if sys.platform.startswith(PLATFORM_WINDOWS):
        return OperatingSystem.WINDOWS
    if sys.platform.startswith(PLATFORM_LINUX):
        return OperatingSystem.LINUX
    if sys.platform.startswith(PLATFORM_DARWIN):
        return OperatingSystem.MACOS
    return OperatingSystem.UNKNOWN


def ensure_utf8_streams() -> None:
    """Force stdout/stderr to UTF-8 so non-ASCII output never crashes on Windows.

    Windows consoles default to a legacy code page (commonly cp1252). OmniForge's
    log format and many messages use ``—``, ``→``, ``…`` and ``⚒``, which raise
    ``UnicodeEncodeError`` when written to such a console — turning ordinary
    logging into a wall of ``--- Logging error ---`` tracebacks that makes a
    working app look broken. Reconfiguring the streams to UTF-8 with a safe
    fallback makes console output identical and crash-free on every OS.

    A no-op on streams that cannot be reconfigured (already decoded, redirected
    to a pipe, or replaced by a test harness), so it is always safe to call.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Detached or non-reconfigurable stream — leave it as-is.
            continue


def _windows_release() -> str:
    """Return the marketing release for Windows, correcting for Python's report.

    ``platform.release()`` still answers ``"10"`` on Windows 11, because the
    kernel version never moved past 10.0. The build number is what actually
    distinguishes them.

    Returns:
        ``"11"`` on build 22000 or later, otherwise whatever platform reports.
    """
    reported = platform.release()
    try:
        build = int(platform.version().split(".")[-1])
    except (ValueError, IndexError):
        return reported
    return "11" if build >= WINDOWS_11_MIN_BUILD else reported


def describe_host() -> HostInfo:
    """Gather a full description of the host machine.

    Returns:
        A populated :class:`HostInfo`. Fields that the platform declines to
        report come back as empty strings rather than raising.
    """
    detected = current_os()
    release = _windows_release() if detected is OperatingSystem.WINDOWS else platform.release()
    return HostInfo(
        os=detected,
        display_name=OS_DISPLAY_NAMES.get(detected.value, detected.value.title()),
        release=release,
        version=platform.version(),
        architecture=platform.machine(),
        python_version=platform.python_version(),
        hostname=platform.node(),
    )


def is_windows() -> bool:
    """Return True when running on Windows."""
    return current_os() is OperatingSystem.WINDOWS


def is_linux() -> bool:
    """Return True when running on Linux."""
    return current_os() is OperatingSystem.LINUX


def is_macos() -> bool:
    """Return True when running on macOS."""
    return current_os() is OperatingSystem.MACOS


def memory_terminology() -> dict[str, str]:
    """Return the OS-appropriate names for memory categories.

    The same underlying counter is called different things on each platform,
    and showing the wrong name makes the reading look wrong. Windows has no
    "cached" figure in the Linux sense, and macOS reports "wired" memory that
    the other two do not.

    Returns:
        Mapping of internal key to the label to display.
    """
    detected = current_os()
    if detected is OperatingSystem.WINDOWS:
        return {"available": "Available", "cached": "Cached", "swap": "Page File"}
    if detected is OperatingSystem.MACOS:
        return {"available": "Available", "cached": "Wired + Cached", "swap": "Swap"}
    return {"available": "Available", "cached": "Buff/Cache", "swap": "Swap"}


def bytes_to_gib(num_bytes: int | float) -> float:
    """Convert a byte count to gibibytes.

    Args:
        num_bytes: The value to convert.

    Returns:
        The value in GiB.
    """
    return float(num_bytes) / (BYTES_PER_UNIT**3)
