"""Pydantic v2 models for the bulk_renamer module."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from modules.extractors.bulk_renamer.constants import DEFAULT_EXCLUDE_PATTERNS
from shared.validators import compile_regex


class RenamePreviewEntry(BaseModel):
    """One file's proposed outcome in a preview or rename run.

    Declared before :class:`RenameParams` because that model embeds a list of
    these as its approved plan.

    Attributes:
        original_path: The file's current path.
        original_name: The file's current name.
        proposed_name: The name it would take (unchanged if unmatched).
        matched: Whether the pattern matched this file's stem at all.
        conflict: True when the proposed name collides with another file's
            proposed name, or with an existing, different, untouched file.
        unsafe: True when the proposed name is not valid on this OS.
        reason: Human-readable note — set for no-op, conflict, unsafe or an
            invalid replacement.
    """

    original_path: Path
    original_name: str
    proposed_name: str
    matched: bool = False
    conflict: bool = False
    unsafe: bool = False
    reason: str | None = None

    @property
    def would_rename(self) -> bool:
        """True when this entry is a clean, safe, actual name change."""
        return (
            self.matched
            and not self.conflict
            and not self.unsafe
            and self.proposed_name != self.original_name
        )


class RenameParams(BaseModel):
    """Input parameters for a preview or a rename run.

    Attributes:
        source_dir: Directory to rename files in.
        pattern: Regular expression matched against each file's stem (the
            name without its extension — the extension is always preserved).
        replacement: ``re.sub`` replacement template. May use backreferences
            (``\\1``, ``\\g<name>``) plus ``{n}`` / ``{n:03d}`` (a per-match
            counter) and ``{date}`` (today's date). Literal braces must be
            escaped as ``{{`` / ``}}``.
        recursive: Descend into subdirectories. When False, only files
            directly inside *source_dir* are considered.
        exclude_patterns: Gitignore-style patterns to skip (recursive only).
        plan: The exact preview the user approved. A rename run carries the
            entries that were on screen when Run was pressed and executes
            those; it does not recompute them. Recomputing meant a file
            created between Preview and Run was renamed without ever being
            shown, and the ``{n}`` counter shifted under the user. Empty on a
            preview request, and on a rename request that has no approved
            preview to honour (the logic then computes one).
    """

    source_dir: Path
    pattern: str
    replacement: str
    recursive: bool = False
    exclude_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS)
    )
    plan: list[RenamePreviewEntry] = Field(default_factory=list)

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

    @field_validator("pattern")
    @classmethod
    def pattern_must_compile(cls, value: str) -> str:
        """Validate that the pattern is a usable regular expression.

        Args:
            value: The regex source string.

        Returns:
            The validated pattern.

        Raises:
            ValueError: When the pattern does not compile.
        """
        compile_regex(value)
        return value


class PreviewResult(BaseModel):
    """What a preview run found.

    Attributes:
        entries: One entry per candidate file, in scan order.
        total_files: Files considered (after the exclude filter).
        matched_count: Files the pattern matched.
        renameable_count: Files that would actually be renamed if run now.
    """

    entries: list[RenamePreviewEntry] = Field(default_factory=list)
    total_files: int = Field(default=0, ge=0)
    matched_count: int = Field(default=0, ge=0)
    renameable_count: int = Field(default=0, ge=0)


class RenamedPair(BaseModel):
    """A single completed rename, kept so it can be undone.

    Attributes:
        old_path: The path before the rename.
        new_path: The path after the rename.
    """

    old_path: Path
    new_path: Path


class UndoParams(BaseModel):
    """Input parameters for reversing a previous rename run.

    Attributes:
        pairs: The pairs to reverse, normally a previous
            ``RenameResult.renamed``.
    """

    pairs: list[RenamedPair]


class RenameResult(BaseModel):
    """Outcome of a completed rename or undo run.

    Attributes:
        files_renamed: Number of files successfully renamed.
        skipped_count: Matched files that were not renamed (conflict, unsafe,
            or a filesystem error).
        warnings: Non-fatal notes, one per skipped file.
        renamed: Every rename actually performed, in reverse order of the
            pairs an undo of this result would need to apply, so the UI can
            offer to undo this exact run.
    """

    files_renamed: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    renamed: list[RenamedPair] = Field(default_factory=list)
