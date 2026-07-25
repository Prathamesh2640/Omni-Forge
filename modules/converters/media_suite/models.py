"""Pydantic v2 models for the media_suite module."""
from __future__ import annotations

import enum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from modules.converters.media_suite.constants import (
    AUDIO_EXTENSIONS,
    DEFAULT_THUMBNAIL_COUNT,
    MAX_THUMBNAIL_COUNT,
    MEDIA_EXTENSIONS,
    MIN_THUMBNAIL_COUNT,
    VIDEO_EXTENSIONS,
)


class MediaOperation(enum.StrEnum):
    """The operations media_suite can perform."""

    COMPRESS_VIDEO = "compress_video"
    EXTRACT_AUDIO = "extract_audio"
    TO_MP4 = "to_mp4"
    THUMBNAILS = "thumbnails"
    NORMALIZE_AUDIO = "normalize_audio"


class SizePreset(enum.StrEnum):
    """Upload ceilings the compressor targets."""

    DISCORD = "discord"
    EMAIL = "email"
    WEB = "web"
    CUSTOM = "custom"


#: Extensions each operation accepts, used by the file picker's filter.
INPUT_EXTENSIONS: dict[MediaOperation, tuple[str, ...]] = {
    MediaOperation.COMPRESS_VIDEO: VIDEO_EXTENSIONS,
    MediaOperation.EXTRACT_AUDIO: VIDEO_EXTENSIONS,
    MediaOperation.TO_MP4: VIDEO_EXTENSIONS,
    MediaOperation.THUMBNAILS: VIDEO_EXTENSIONS,
    MediaOperation.NORMALIZE_AUDIO: MEDIA_EXTENSIONS,
}


class MediaInfo(BaseModel):
    """What ffprobe reports about a media file.

    Attributes:
        duration_seconds: Playback length. Zero when unknown.
        width: Video width in pixels, or 0 for audio-only files.
        height: Video height in pixels, or 0 for audio-only files.
        has_audio: Whether an audio stream is present.
    """

    duration_seconds: float = Field(default=0.0, ge=0.0)
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    has_audio: bool = False


class MediaParams(BaseModel):
    """Input parameters for a media_suite execution.

    Attributes:
        operation: Which operation to run.
        input_paths: Source files; each is processed independently.
        output_dir: Directory the results are written to.
        preset: Upload ceiling to target when compressing.
        target_mb: Explicit size ceiling when *preset* is CUSTOM.
        thumbnail_count: How many stills to extract.
    """

    operation: MediaOperation
    input_paths: list[Path] = Field(min_length=1)
    output_dir: Path

    preset: SizePreset = SizePreset.DISCORD
    target_mb: int = Field(default=10, gt=0)
    thumbnail_count: int = Field(
        default=DEFAULT_THUMBNAIL_COUNT,
        ge=MIN_THUMBNAIL_COUNT,
        le=MAX_THUMBNAIL_COUNT,
    )

    @model_validator(mode="after")
    def check_inputs(self) -> MediaParams:
        """Reject inputs the chosen operation cannot read.

        Returns:
            The validated model.

        Raises:
            ValueError: When a file's extension does not suit the operation.
        """
        accepted = INPUT_EXTENSIONS[self.operation]
        for path in self.input_paths:
            if path.suffix.lower() not in accepted:
                kind = "video" if accepted is VIDEO_EXTENSIONS else "media"
                raise ValueError(
                    f"{path.name} is not a {kind} file that "
                    f"{self.operation.value.replace('_', ' ')} can read."
                )
        return self

    @property
    def is_audio_only_input(self) -> bool:
        """True when every input is an audio file."""
        return all(p.suffix.lower() in AUDIO_EXTENSIONS for p in self.input_paths)


class MediaResult(BaseModel):
    """Outcome of a completed media_suite run.

    Attributes:
        operation: The operation that ran.
        output_paths: Files or directories produced.
        files_processed: Number of source files handled.
        input_bytes: Combined size of the inputs.
        output_bytes: Combined size of the outputs.
        detail: Short human-readable summary.
        warnings: Non-fatal notes, e.g. a target size that was not reached.
    """

    operation: MediaOperation
    output_paths: list[Path]
    files_processed: int = Field(ge=0)
    input_bytes: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    detail: str = ""
    warnings: list[str] = Field(default_factory=list)

    @property
    def bytes_saved(self) -> int:
        """Reduction in size. Negative when the output grew."""
        return self.input_bytes - self.output_bytes

    @property
    def size_change_percent(self) -> float:
        """Percentage change in size; 0.0 when the input was empty."""
        if self.input_bytes == 0:
            return 0.0
        return (self.bytes_saved / self.input_bytes) * 100.0
