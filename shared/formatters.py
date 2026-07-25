"""Human-readable formatting helpers shared by every module.

Used for destructive-operation impact summaries (rule B-03), progress
messages, and table cells — e.g. ``"4.2 GB across 1,247 files"``.

Sizes use binary multiples (1 KB = 1024 B), matching what desktop file
managers report.
"""
from __future__ import annotations

from pathlib import Path

from shared.constants import (
    BYTES_PER_UNIT,
    SECONDS_PER_HOUR,
    SECONDS_PER_MINUTE,
    SIZE_UNITS,
)

_ELLIPSIS = "…"


def format_bytes(num_bytes: int, precision: int = 1) -> str:
    """Format a byte count as a human-readable size string.

    Args:
        num_bytes: Number of bytes. Negative values are rendered with a
            leading minus sign (useful for "space freed" deltas).
        precision: Decimal places for units above bytes.

    Returns:
        A string such as ``"0 B"``, ``"512 B"``, ``"1.4 KB"``, ``"4.2 GB"``.
    """
    sign = "-" if num_bytes < 0 else ""
    size = float(abs(num_bytes))

    for unit in SIZE_UNITS:
        if size < BYTES_PER_UNIT or unit == SIZE_UNITS[-1]:
            # Bytes are always whole; larger units carry decimals.
            if unit == SIZE_UNITS[0]:
                return f"{sign}{int(size)} {unit}"
            return f"{sign}{size:.{precision}f} {unit}"
        size /= BYTES_PER_UNIT

    raise AssertionError("unreachable — SIZE_UNITS is non-empty")


def format_duration(seconds: float) -> str:
    """Format a duration as a compact human-readable string.

    Args:
        seconds: Elapsed time in seconds.

    Returns:
        A string such as ``"0.4s"``, ``"12.3s"``, ``"5m 03s"``, ``"2h 15m"``.
    """
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds:.1f}s"
    if seconds < SECONDS_PER_HOUR:
        minutes, secs = divmod(int(seconds), SECONDS_PER_MINUTE)
        return f"{minutes}m {secs:02d}s"
    hours, remainder = divmod(int(seconds), SECONDS_PER_HOUR)
    return f"{hours}h {remainder // SECONDS_PER_MINUTE:02d}m"


def format_impact(num_bytes: int, file_count: int) -> str:
    """Build the impact summary shown before a destructive operation (rule B-03).

    Args:
        num_bytes: Total bytes affected.
        file_count: Number of files affected.

    Returns:
        A string such as ``"4.2 GB across 1,247 files"`` (singular for one file).
    """
    noun = "file" if file_count == 1 else "files"
    return f"{format_bytes(num_bytes)} across {file_count:,} {noun}"


def truncate_path(path: Path | str, max_chars: int) -> str:
    """Shorten a path from the left, keeping the filename readable.

    Args:
        path: Path to shorten.
        max_chars: Maximum length of the returned string. Values below 1
            are treated as 1.

    Returns:
        The path unchanged when short enough, otherwise an ellipsis-prefixed
        tail such as ``"…/deep/dir/report.pdf"``.
    """
    text = str(path)
    limit = max(max_chars, 1)
    if len(text) <= limit:
        return text
    if limit == 1:
        # text[-0:] would slice the whole string, not nothing.
        return _ELLIPSIS
    return _ELLIPSIS + text[-(limit - 1) :]
