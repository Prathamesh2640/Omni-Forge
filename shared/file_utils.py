"""Safe filesystem helpers shared by every module.

Hashing streams files in chunks so multi-gigabyte inputs never load into
memory. Copy and move never overwrite silently — collisions get a numeric
suffix, and the caller is told the real destination.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import xxhash

from shared.constants import HASH_CHUNK_SIZE_BYTES, LOG_ROOT_NAME

# ``shared`` sits below ``core`` in the dependency order (rule A-07), and
# ``core.recycle_store`` imports this module — importing ``core.logger`` here
# would close that loop. Attaching to the same logger tree by name keeps the
# output identical without the import.
logger = logging.getLogger(f"{LOG_ROOT_NAME}.shared.file_utils")


class _Hasher(Protocol):
    """The subset of the hashlib interface this module relies on."""

    def update(self, data: bytes, /) -> None:
        """Feed *data* into the running digest."""
        ...

    def hexdigest(self) -> str:
        """Return the digest as a lowercase hex string."""
        ...


#: Supported hash algorithms mapped to their constructors.
#: xxh3_128 is the fast non-cryptographic default used by duplicate detection;
#: the SHA family is for integrity verification. MD5 and SHA-1 are offered for
#: *comparing against externally published checksums only* — never for
#: security decisions (rule C-06).
HASH_ALGORITHMS: dict[str, Callable[[], _Hasher]] = {
    "xxh3_128": xxhash.xxh3_128,
    "md5": hashlib.md5,
    "sha1": hashlib.sha1,
    "sha256": hashlib.sha256,
    "sha512": hashlib.sha512,
}

DEFAULT_HASH_ALGORITHM = "xxh3_128"


def hash_file(path: Path, algorithm: str = DEFAULT_HASH_ALGORITHM) -> str:
    """Compute the hex digest of a file's contents.

    Args:
        path: File to hash.
        algorithm: A key of :data:`HASH_ALGORITHMS`.

    Returns:
        Lowercase hexadecimal digest.

    Raises:
        ValueError: When *algorithm* is not supported.
        OSError: When *path* cannot be read.
    """
    if algorithm not in HASH_ALGORITHMS:
        supported = ", ".join(sorted(HASH_ALGORITHMS))
        raise ValueError(f"Unsupported hash algorithm {algorithm!r}. Supported: {supported}")

    digest = HASH_ALGORITHMS[algorithm]()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_SIZE_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def directory_size(path: Path) -> tuple[int, int]:
    """Measure a directory tree, for destructive-operation previews (rule B-03).

    Unreadable entries are skipped and logged rather than aborting the walk —
    a single permission-denied subdirectory must not break the estimate.

    Args:
        path: Directory to measure. A file path measures just that file.

    Returns:
        A ``(total_bytes, file_count)`` tuple. ``(0, 0)`` if *path* is absent.
    """
    if path.is_file():
        return path.stat().st_size, 1
    if not path.is_dir():
        return 0, 0

    total_bytes = 0
    file_count = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            for entry in current.iterdir():
                # Do not follow symlinks — they can escape the tree or loop.
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    total_bytes += entry.stat().st_size
                    file_count += 1
        except OSError as exc:
            logger.debug("file_utils.scan_skip — path=%s reason=%s", current, exc)
    return total_bytes, file_count


def unique_destination(destination: Path) -> Path:
    """Return a non-colliding variant of *destination*.

    ``report.pdf`` becomes ``report (1).pdf``, then ``report (2).pdf``, and so
    on until an unused name is found.

    Args:
        destination: The desired destination path.

    Returns:
        *destination* itself when free, otherwise a suffixed sibling.
    """
    if not destination.exists():
        return destination
    counter = 1
    while True:
        candidate = destination.with_name(
            f"{destination.stem} ({counter}){destination.suffix}"
        )
        if not candidate.exists():
            return candidate
        counter += 1


def safe_copy(source: Path, destination: Path) -> Path:
    """Copy *source* to *destination*, never overwriting an existing file.

    Args:
        source: File to copy.
        destination: Desired destination path.

    Returns:
        The path actually written, which may carry a ``(n)`` suffix.

    Raises:
        OSError: When the copy fails.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = unique_destination(destination)
    shutil.copy2(source, target)
    logger.debug("file_utils.copied — to=%s", target)
    return target


def safe_move(source: Path, destination: Path) -> Path:
    """Move *source* to *destination*, never overwriting an existing file.

    Falls back to copy-then-delete when the paths span filesystems.

    Args:
        source: File or directory to move.
        destination: Desired destination path.

    Returns:
        The path actually written, which may carry a ``(n)`` suffix.

    Raises:
        OSError: When the move fails.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = unique_destination(destination)
    shutil.move(str(source), str(target))
    logger.debug("file_utils.moved — to=%s", target)
    return target
