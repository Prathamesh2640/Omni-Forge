"""Pydantic v2 models for the duplicate_finder module."""
from __future__ import annotations

import datetime
import enum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from modules.extractors.duplicate_finder.constants import (
    DEFAULT_EXCLUDE_PATTERNS,
    DEFAULT_MIN_SIZE_BYTES,
)


class KeepStrategy(enum.StrEnum):
    """Which file in a duplicate group survives."""

    NEWEST = "newest"
    OLDEST = "oldest"
    MANUAL = "manual"


class ScanParams(BaseModel):
    """Input parameters for a duplicate scan.

    Attributes:
        source_dir: Root directory to scan.
        exclude_patterns: Gitignore-style patterns to skip.
        min_size_bytes: Files smaller than this are ignored (default skips
            zero-byte files, which reclaim nothing and would otherwise form
            one enormous, meaningless group).
    """

    source_dir: Path
    exclude_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS)
    )
    min_size_bytes: int = Field(default=DEFAULT_MIN_SIZE_BYTES, ge=0)

    @field_validator("source_dir")
    @classmethod
    def source_must_exist(cls, value: Path) -> Path:
        """Validate that the source directory exists.

        Args:
            value: Path to validate.

        Returns:
            The validated path.

        Raises:
            ValueError: When the directory is absent.
        """
        if not value.is_dir():
            raise ValueError(f"Not a directory: {value}")
        return value


class DuplicateFile(BaseModel):
    """One file belonging to a duplicate group.

    Attributes:
        path: Absolute path to the file.
        size_bytes: File size in bytes.
        modified_at: Last-modified timestamp, used to pick newest/oldest.
    """

    path: Path
    size_bytes: int = Field(ge=0)
    modified_at: datetime.datetime


class DuplicateGroup(BaseModel):
    """A set of two or more files sharing identical content.

    Attributes:
        content_hash: The xxh3_128 digest shared by every file in the group.
        size_bytes: Size of one file (all members share this size).
        files: Every file with this content, most-recently-modified first.
    """

    content_hash: str
    size_bytes: int = Field(ge=0)
    files: list[DuplicateFile]

    @property
    def wasted_bytes(self) -> int:
        """Space reclaimable by keeping exactly one copy."""
        return self.size_bytes * (len(self.files) - 1)


class ScanResult(BaseModel):
    """What a duplicate scan found.

    Attributes:
        groups: Duplicate groups, largest waste first.
        total_files_scanned: Files considered (after size and exclude filters).
        total_wasted_bytes: Combined reclaimable space across every group.
        scan_duration_ms: Wall time the scan took, for diagnostics.
    """

    groups: list[DuplicateGroup] = Field(default_factory=list)
    total_files_scanned: int = Field(default=0, ge=0)
    total_wasted_bytes: int = Field(default=0, ge=0)
    scan_duration_ms: float = Field(default=0.0, ge=0.0)


class ResolveParams(BaseModel):
    """Input parameters for deleting duplicates down to one copy per group.

    Attributes:
        groups: The groups to resolve (normally a previous ScanResult.groups).
        strategy: Which file survives in every group.
        manual_keep: Required only when *strategy* is MANUAL — maps each
            group's content_hash to the path that should survive.
    """

    groups: list[DuplicateGroup]
    strategy: KeepStrategy = KeepStrategy.NEWEST
    manual_keep: dict[str, Path] = Field(default_factory=dict)


class ResolveResult(BaseModel):
    """Outcome of a completed duplicate-resolution run.

    Attributes:
        files_deleted: Number of files actually recycled.
        bytes_pending_release: Combined size of the recycled files. This is
            **not** free space yet: every deletion routes through the recycle
            store (rule B-04), which keeps the payload on the source's own
            volume so the move is a rename rather than a copy. The space comes
            back when the 24-hour undo window expires, or when the user empties
            the Recycle Bin. Reporting it as "reclaimed" told the user gigabytes
            had been recovered while their free space had not moved.
        recycle_batch_id: Undo handle — every deletion routes through the
            recycle store (rule B-04).
        warnings: Groups that could not be resolved (e.g. a MANUAL choice
            that did not name a real member of the group).
    """

    files_deleted: int = Field(default=0, ge=0)
    bytes_pending_release: int = Field(default=0, ge=0)
    recycle_batch_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
