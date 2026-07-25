"""Input validation helpers shared by every module.

The write-target helpers enforce rule B-07: a module may only write to
``exports/``, ``temp/``, or a directory the user explicitly chose. Modules
pass a user-selected directory via the *extra_roots* argument.
"""
from __future__ import annotations

import re
from pathlib import Path

from shared.constants import (
    EXPORTS_DIR,
    RESERVED_FILENAME_CHARS,
    TEMP_DIR,
    WINDOWS_RESERVED_NAMES,
)


def normalise_extension(ext: str) -> str:
    """Lowercase an extension and ensure it is dot-prefixed.

    Args:
        ext: Raw extension, with or without a leading dot (``"PY"``, ``".Py"``).

    Returns:
        The normalised extension (``".py"``), or an empty string when *ext*
        is blank.
    """
    cleaned = ext.strip().lower()
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


def compile_regex(pattern: str) -> re.Pattern[str]:
    """Compile a user-supplied regular expression.

    Args:
        pattern: The regex source string.

    Returns:
        The compiled pattern.

    Raises:
        ValueError: When *pattern* is not a valid regular expression. The
            message is safe to show directly in the UI.
    """
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc


def is_within(candidate: Path, root: Path) -> bool:
    """Return True when *candidate* is *root* itself or nested inside it.

    Both paths are resolved first, so symlinks and ``..`` segments cannot be
    used to escape *root*.

    Args:
        candidate: The path being checked.
        root: The directory that should contain *candidate*.

    Returns:
        True if contained, False otherwise (including on resolution errors).
    """
    try:
        candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def validate_write_target(path: Path, extra_roots: tuple[Path, ...] = ()) -> Path:
    """Assert that *path* is a permitted write location (rule B-07).

    Args:
        path: The file or directory a module intends to write.
        extra_roots: Additional permitted roots — pass the directory the
            user explicitly selected for this operation.

    Returns:
        The resolved path, safe to write.

    Raises:
        ValueError: When *path* falls outside ``exports/``, ``temp/``, and
            every directory in *extra_roots*.
    """
    allowed = (Path(EXPORTS_DIR), Path(TEMP_DIR), *extra_roots)
    if any(is_within(path, root) for root in allowed):
        return path.resolve()
    permitted = ", ".join(str(root) for root in allowed)
    raise ValueError(f"Refusing to write outside permitted directories ({permitted}): {path}")


def is_safe_filename(name: str) -> bool:
    """Return True when *name* is usable as a filename on every platform.

    Rejects empty names, path separators, Windows-reserved device names,
    reserved characters, and trailing dots or spaces.

    Args:
        name: The candidate filename (no directory component).

    Returns:
        True when the name is safe to create on disk.
    """
    if not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name:
        return False
    if any(char in name for char in RESERVED_FILENAME_CHARS):
        return False
    if name[-1] in {".", " "}:
        return False
    return name.split(".")[0].upper() not in WINDOWS_RESERVED_NAMES
