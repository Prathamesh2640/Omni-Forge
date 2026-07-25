"""Constants for the media_suite module (rule D-05 — no magic numbers)."""
from __future__ import annotations

# ─── EventBus topics ──────────────────────────────────────────────────────────
EVENT_EXECUTE: str = "converters.media_suite.execute"
EVENT_PROGRESS: str = "converters.media_suite.progress"
EVENT_DONE: str = "converters.media_suite.done"
EVENT_CANCEL: str = "converters.media_suite.cancel"
EVENT_CANCELLED: str = "converters.media_suite.cancelled"
EVENT_ERROR: str = "converters.media_suite.error"

# ─── Storage ──────────────────────────────────────────────────────────────────
STORAGE_TABLE: str = "converters.media_suite"
STORAGE_KEY_LAST_DIR: str = "last_input_dir"
STORAGE_KEY_LAST_OPERATION: str = "last_operation"

# ─── Output ───────────────────────────────────────────────────────────────────
OUTPUT_SUBDIR: str = "media_suite"
PROGRESS_COMPLETE: int = 100
THUMBNAIL_DIR_TEMPLATE: str = "{stem}_thumbnails"
THUMBNAIL_FILENAME_TEMPLATE: str = "{stem}_%03d.jpg"

# ─── External binaries ────────────────────────────────────────────────────────
FFMPEG_BINARY: str = "ffmpeg"
FFPROBE_BINARY: str = "ffprobe"

# Bundled binaries are searched before PATH, so a packaged build is
# self-contained (Phase 8 ships these).
BUNDLED_DIR: str = "bundled"

FFMPEG_INSTALL_HINT: str = (
    "Media Suite needs FFmpeg, which was not found in bundled/ or on PATH.\n"
    "Windows:  winget install Gyan.FFmpeg\n"
    "macOS:    brew install ffmpeg\n"
    "Linux:    sudo apt install ffmpeg\n"
    "Then restart OmniForge."
)

# ─── Input formats ────────────────────────────────────────────────────────────
VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".flv", ".wmv",
)
AUDIO_EXTENSIONS: tuple[str, ...] = (".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg")
MEDIA_EXTENSIONS: tuple[str, ...] = VIDEO_EXTENSIONS + AUDIO_EXTENSIONS

# ─── Compression presets ──────────────────────────────────────────────────────
# Target upload ceilings, in mebibytes.
PRESET_TARGET_MB: dict[str, int] = {
    "discord": 10,
    "email": 25,
    "web": 50,
}

# Reserved for the audio track and container overhead when computing the video
# bitrate for a target size.
AUDIO_BITRATE_KBPS: int = 128
CONTAINER_OVERHEAD_FRACTION: float = 0.05
MIN_VIDEO_BITRATE_KBPS: int = 100

# x264 preset trading encode speed against compression efficiency.
X264_PRESET: str = "medium"
X264_CRF: int = 23

# ─── Audio ────────────────────────────────────────────────────────────────────
MP3_BITRATE: str = "192k"
# EBU R128 integrated loudness target, in LUFS — the broadcast standard.
LOUDNESS_TARGET_LUFS: float = -16.0
LOUDNESS_TRUE_PEAK_DB: float = -1.5
LOUDNESS_RANGE_LU: float = 11.0

# ─── Thumbnails ───────────────────────────────────────────────────────────────
DEFAULT_THUMBNAIL_COUNT: int = 6
MIN_THUMBNAIL_COUNT: int = 1
MAX_THUMBNAIL_COUNT: int = 50
THUMBNAIL_WIDTH_PX: int = 480

# ─── Execution ────────────────────────────────────────────────────────────────
# Ceiling for a single FFmpeg invocation. Long encodes are legitimate, so this
# is generous; the watchdog exists to catch a wedged process, not a slow one.
FFMPEG_TIMEOUT_SECONDS: int = 3600
# How long to wait for the stderr reader thread to finish once FFmpeg has
# exited. Its pipe is at EOF by then, so this only bounds a pathological case.
STREAM_DRAIN_JOIN_SECONDS: float = 5.0
BYTES_PER_MIB: int = 1024 * 1024
BITS_PER_KILOBIT: int = 1000

# ─── UI ───────────────────────────────────────────────────────────────────────
FILE_LIST_HEIGHT_PX: int = 180

# Remembers where the user last sent this module's output (rule E-03).
STORAGE_KEY_OUTPUT_DIR: str = "last_output_dir"

# FFmpeg reports output time in microseconds on its -progress stream.
MICROSECONDS_PER_SECOND: int = 1_000_000
# An encode is held just below completion until the file is actually closed,
# so the bar never sits at 100% while work is still happening.
MAX_ENCODE_PERCENT: int = 99
