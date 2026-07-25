"""Constants for the pdf_suite module (rule D-05 — no magic numbers)."""
from __future__ import annotations

# ─── EventBus topics ──────────────────────────────────────────────────────────
EVENT_EXECUTE: str = "converters.pdf_suite.execute"
EVENT_PROGRESS: str = "converters.pdf_suite.progress"
EVENT_DONE: str = "converters.pdf_suite.done"
EVENT_CANCEL: str = "converters.pdf_suite.cancel"
EVENT_CANCELLED: str = "converters.pdf_suite.cancelled"
EVENT_ERROR: str = "converters.pdf_suite.error"

# ─── Storage ──────────────────────────────────────────────────────────────────
STORAGE_TABLE: str = "converters.pdf_suite"
STORAGE_KEY_LAST_DIR: str = "last_input_dir"
STORAGE_KEY_LAST_OPERATION: str = "last_operation"

# ─── Output ───────────────────────────────────────────────────────────────────
OUTPUT_SUBDIR: str = "pdf_suite"
MERGED_FILENAME_TEMPLATE: str = "merged_{timestamp}.pdf"
SPLIT_FILENAME_TEMPLATE: str = "{stem}_part{index:03d}.pdf"
COMPRESSED_FILENAME_TEMPLATE: str = "{stem}_compressed.pdf"
DECRYPTED_FILENAME_TEMPLATE: str = "{stem}_decrypted.pdf"
# Scratch copy used to hand an encrypted source to a library that cannot open
# one (pdf2docx). Always deleted once the conversion finishes.
TEMP_DECRYPTED_TEMPLATE: str = "{stem}_decrypted_tmp.pdf"
METADATA_FILENAME_TEMPLATE: str = "{stem}_updated.pdf"
ROTATED_FILENAME_TEMPLATE: str = "{stem}_rotated.pdf"
TEXT_FILENAME_TEMPLATE: str = "{stem}.txt"
DOCX_FILENAME_TEMPLATE: str = "{stem}.docx"
IMAGES_SUBDIR_TEMPLATE: str = "{stem}_images"
IMAGE_FILENAME_TEMPLATE: str = "page{page:03d}_img{index:02d}.{ext}"
TIMESTAMP_FORMAT: str = "%Y%m%d_%H%M%S"

# ─── Progress checkpoints ─────────────────────────────────────────────────────
PROGRESS_START: int = 0
PROGRESS_VALIDATED: int = 5
PROGRESS_WORK_START: int = 10
PROGRESS_WORK_END: int = 90
PROGRESS_COMPLETE: int = 100

# ─── Compression presets ──────────────────────────────────────────────────────
# Target DPI for embedded raster images. Text and vectors are never resampled,
# so these only affect scanned or image-heavy documents.
PRESET_IMAGE_DPI: dict[str, int] = {
    "screen": 72,
    "ebook": 150,
    "print": 300,
}

# JPEG quality applied when a raster image is downsampled.
PRESET_JPEG_QUALITY: dict[str, int] = {
    "screen": 60,
    "ebook": 80,
    "print": 92,
}

# Images smaller than this are left untouched — recompressing icons and
# rules costs quality for no measurable saving.
MIN_IMAGE_PIXELS_TO_RESAMPLE: int = 10_000

# ─── Rotation ─────────────────────────────────────────────────────────────────
VALID_ROTATIONS: tuple[int, ...] = (0, 90, 180, 270)
DEGREES_IN_CIRCLE: int = 360

# ─── Splitting ────────────────────────────────────────────────────────────────
MIN_PAGES_PER_CHUNK: int = 1

# ─── Metadata ─────────────────────────────────────────────────────────────────
# The PDF metadata fields this module exposes for editing.
EDITABLE_METADATA_FIELDS: tuple[str, ...] = (
    "title",
    "author",
    "subject",
    "keywords",
    "creator",
    "producer",
)

# ─── External tooling ─────────────────────────────────────────────────────────
TESSERACT_BINARY: str = "tesseract"
TESSERACT_INSTALL_HINT: str = (
    "OCR requires the Tesseract engine, which was not found on PATH.\n"
    "Windows:  winget install UB-Mannheim.TesseractOCR\n"
    "macOS:    brew install tesseract\n"
    "Linux:    sudo apt install tesseract-ocr"
)

# ─── UI ───────────────────────────────────────────────────────────────────────
FILE_LIST_HEIGHT_PX: int = 200

# Extensions offered by the file picker, and the label for its filter group.
PDF_EXTENSIONS: tuple[str, ...] = (".pdf",)
PDF_FILE_TYPE_LABEL: str = "PDF documents"
MAX_PREVIEW_PAGES: int = 5

# Remembers where the user last sent this module's output (rule E-03).
STORAGE_KEY_OUTPUT_DIR: str = "last_output_dir"
