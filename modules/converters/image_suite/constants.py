"""Constants for the image_suite module (rule D-05 — no magic numbers)."""
from __future__ import annotations

# ─── EventBus topics ──────────────────────────────────────────────────────────
EVENT_EXECUTE: str = "converters.image_suite.execute"
EVENT_PROGRESS: str = "converters.image_suite.progress"
EVENT_DONE: str = "converters.image_suite.done"
EVENT_CANCEL: str = "converters.image_suite.cancel"
EVENT_CANCELLED: str = "converters.image_suite.cancelled"
EVENT_ERROR: str = "converters.image_suite.error"

# ─── Storage ──────────────────────────────────────────────────────────────────
STORAGE_TABLE: str = "converters.image_suite"
STORAGE_KEY_LAST_DIR: str = "last_input_dir"
STORAGE_KEY_LAST_OPERATION: str = "last_operation"

# ─── Output ───────────────────────────────────────────────────────────────────
OUTPUT_SUBDIR: str = "image_suite"
PROGRESS_COMPLETE: int = 100

# ─── Input formats ────────────────────────────────────────────────────────────
RASTER_EXTENSIONS: tuple[str, ...] = (
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif", ".heic", ".heif",
)
SVG_EXTENSIONS: tuple[str, ...] = (".svg",)

# Extensions that only pillow-heif can open.
HEIF_EXTENSIONS: tuple[str, ...] = (".heic", ".heif")

# ─── Output formats ───────────────────────────────────────────────────────────
# Pillow's format name and file extension for each target the UI offers.
OUTPUT_FORMATS: dict[str, tuple[str, str]] = {
    "jpeg": ("JPEG", ".jpg"),
    "png": ("PNG", ".png"),
    "webp": ("WEBP", ".webp"),
}

# Formats that cannot store an alpha channel, so transparency is flattened.
OPAQUE_FORMATS: frozenset[str] = frozenset({"JPEG"})

# Background painted behind transparent pixels when flattening.
FLATTEN_BACKGROUND: tuple[int, int, int] = (255, 255, 255)

# ─── Quality ──────────────────────────────────────────────────────────────────
DEFAULT_QUALITY: int = 85
MIN_QUALITY: int = 5
MAX_QUALITY: int = 95

# Binary search bounds when compressing to a target file size.
TARGET_SIZE_MAX_ATTEMPTS: int = 12
# Stop once within this fraction of the target, to avoid pointless extra passes.
TARGET_SIZE_TOLERANCE: float = 0.05

# ─── Resizing ─────────────────────────────────────────────────────────────────
DEFAULT_MAX_DIMENSION: int = 1920
MIN_DIMENSION: int = 16
MAX_DIMENSION: int = 20_000

# ─── SVG rasterisation ────────────────────────────────────────────────────────
# Android density buckets: directory suffix mapped to its scale factor.
ANDROID_DENSITIES: dict[str, float] = {
    "mdpi": 1.0,
    "hdpi": 1.5,
    "xhdpi": 2.0,
    "xxhdpi": 3.0,
    "xxxhdpi": 4.0,
}
ANDROID_DRAWABLE_DIR_TEMPLATE: str = "drawable-{density}"
ANDROID_BASE_SIZE_PX: int = 24

# Standard favicon sizes, in pixels.
FAVICON_SIZES: tuple[int, ...] = (16, 32, 48, 64, 128, 180, 192, 512)
FAVICON_DIR_TEMPLATE: str = "{stem}_favicons"
FAVICON_FILENAME_TEMPLATE: str = "favicon-{size}x{size}.png"
FAVICON_ICO_NAME: str = "favicon.ico"
# Sizes embedded in the multi-resolution .ico bundle.
FAVICON_ICO_SIZES: tuple[int, ...] = (16, 32, 48)

# ─── Android VectorDrawable ───────────────────────────────────────────────────
VECTOR_DRAWABLE_TEMPLATE: str = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
    '    android:width="{width}dp"\n'
    '    android:height="{height}dp"\n'
    '    android:viewportWidth="{viewport_width}"\n'
    '    android:viewportHeight="{viewport_height}">\n'
    "{paths}"
    "</vector>\n"
)
VECTOR_PATH_TEMPLATE: str = (
    "    <path\n"
    '        android:fillColor="{fill}"\n'
    '        android:pathData="{data}"/>\n'
)
VECTOR_DEFAULT_FILL: str = "#FF000000"
SVG_DEFAULT_VIEWPORT: float = 24.0

# ─── EXIF ─────────────────────────────────────────────────────────────────────
# Metadata keys stripped when sanitising an image.
STRIPPED_INFO_KEYS: tuple[str, ...] = ("exif", "icc_profile", "XML:com.adobe.xmp")

# ─── UI ───────────────────────────────────────────────────────────────────────
FILE_LIST_HEIGHT_PX: int = 180
BYTES_PER_KIB: int = 1024
DEFAULT_TARGET_KB: int = 500

# Remembers where the user last sent this module's output (rule E-03).
STORAGE_KEY_OUTPUT_DIR: str = "last_output_dir"
