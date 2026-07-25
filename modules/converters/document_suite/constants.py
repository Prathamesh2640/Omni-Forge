"""Constants for the document_suite module (rule D-05 — no magic numbers)."""
from __future__ import annotations

# ─── EventBus topics ──────────────────────────────────────────────────────────
EVENT_EXECUTE: str = "converters.document_suite.execute"
EVENT_PROGRESS: str = "converters.document_suite.progress"
EVENT_DONE: str = "converters.document_suite.done"
EVENT_CANCEL: str = "converters.document_suite.cancel"
EVENT_CANCELLED: str = "converters.document_suite.cancelled"
EVENT_ERROR: str = "converters.document_suite.error"

# ─── Storage ──────────────────────────────────────────────────────────────────
STORAGE_TABLE: str = "converters.document_suite"
STORAGE_KEY_LAST_DIR: str = "last_input_dir"
STORAGE_KEY_LAST_CONVERSION: str = "last_conversion"

# ─── Output ───────────────────────────────────────────────────────────────────
OUTPUT_SUBDIR: str = "document_suite"

# ─── Progress checkpoints ─────────────────────────────────────────────────────
PROGRESS_START: int = 0
PROGRESS_READ: int = 20
PROGRESS_PARSED: int = 45
PROGRESS_CONVERTED: int = 75
PROGRESS_COMPLETE: int = 100

# ─── Markdown → HTML ──────────────────────────────────────────────────────────
# Pygments style used for the embedded syntax-highlighting stylesheet.
CODE_HIGHLIGHT_STYLE: str = "monokai"

# The output is a single self-contained file — no CDN, no external stylesheet,
# so it renders identically offline (rule C-01).
HTML_DOCUMENT_TEMPLATE: str = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
{base_css}
{code_css}
</style>
</head>
<body>
<main class="omniforge-doc">
{body}
</main>
</body>
</html>
"""

HTML_BASE_CSS: str = """
:root { color-scheme: light dark; }
body { margin: 0; background: #ffffff; color: #1f2328;
       font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       font-size: 16px; line-height: 1.6; }
.omniforge-doc { max-width: 860px; margin: 0 auto; padding: 40px 24px; }
h1, h2, h3, h4 { line-height: 1.25; margin-top: 1.6em; margin-bottom: .6em; }
h1 { font-size: 2em; border-bottom: 1px solid #d1d9e0; padding-bottom: .3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid #d1d9e0; padding-bottom: .3em; }
a { color: #0969da; }
code { font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
       font-size: .875em; background: rgba(129,139,152,.16);
       padding: .2em .4em; border-radius: 6px; }
pre { padding: 16px; overflow: auto; border-radius: 6px; }
pre code { background: transparent; padding: 0; }
blockquote { margin: 0; padding: 0 1em; color: #59636e;
             border-left: .25em solid #d1d9e0; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #d1d9e0; padding: 6px 13px; }
th { background: #f6f8fa; }
img { max-width: 100%; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  h1, h2 { border-bottom-color: #3d444d; }
  a { color: #4493f8; }
  blockquote { color: #9198a1; border-left-color: #3d444d; }
  th, td { border-color: #3d444d; }
  th { background: #151b23; }
}
"""

# ─── Tabular conversion ───────────────────────────────────────────────────────
# Separator joining nested JSON keys when flattening, e.g. "user.address.city".
FLATTEN_SEPARATOR: str = "."

# Worksheet name used for the generated Excel sheet.
EXCEL_SHEET_NAME: str = "Data"

# Column width bounds applied when auto-sizing the Excel output.
EXCEL_MIN_COLUMN_WIDTH: int = 8
EXCEL_MAX_COLUMN_WIDTH: int = 60
EXCEL_WIDTH_PADDING: int = 2

# Values recognised as booleans when inferring CSV column types.
CSV_TRUE_VALUES: frozenset[str] = frozenset({"true", "yes"})
CSV_FALSE_VALUES: frozenset[str] = frozenset({"false", "no"})
CSV_NULL_VALUES: frozenset[str] = frozenset({"", "null", "none", "nan"})

# ─── Serialisation ────────────────────────────────────────────────────────────
JSON_INDENT: int = 2
YAML_INDENT: int = 2
YAML_LINE_WIDTH: int = 100

# ─── UI ───────────────────────────────────────────────────────────────────────
FILE_LIST_HEIGHT_PX: int = 180

# Remembers where the user last sent this module's output (rule E-03).
STORAGE_KEY_OUTPUT_DIR: str = "last_output_dir"
