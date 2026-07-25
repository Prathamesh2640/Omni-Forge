# OmniForge

> The Offline Developer File Toolkit

OmniForge is a cross-platform, fully offline, modular developer file toolkit
built on a Micro-Kernel Plugin Registry architecture. It replaces scattered
terminal one-liners and ad-laden online converters with a single desktop
application that never sends your files anywhere.

**Platform:** Windows 10+ | Ubuntu 20.04+ | macOS 12+
**Python:** 3.11+
**Architecture:** Micro-Kernel Plugin Registry (NiceGUI / FastAPI)
**Version:** 1.0 — "Offline Developer File Toolkit"

---

## What V1 ships

OmniForge is a **versioned product**. V1 is deliberately focused on the two
file-centric pillars — everything here works offline, on all three platforms,
with no external daemon:

- **Converters** — `pdf_suite`, `image_suite`, `media_suite`, `document_suite`
- **Extractors** — `llm_packager` (the flagship), `file_filter`,
  `duplicate_finder`, `bulk_renamer`

Two further tiers are **built and tested but parked** for later releases and are
not part of the V1 install: **V2 — System** (process/RAM/cache/disk tools) and
**V3 — Security** (encryption, secret scanning, network utilities). The registry
gates modules by a `tier` field in each manifest (see
`docs/rfcs/0007-version-tiers.md`), so parked tiers never load or pull their
dependencies.

---

## Quick Start

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

```bash
python app.py
```

That opens OmniForge as a native desktop window. To run in a browser tab instead — useful for debugging, CI, or a machine without the WebView2 Runtime:

```bash
python app.py --browser
```

### How the two modes differ

OmniForge is a local Python application, not a web app. The Python process runs
with your full user privileges and does all the real work; the window — whether
it is a native frame or a browser tab — only paints pixels and talks to
`127.0.0.1:8765` over a websocket.

```
┌─ Python process — full OS access ────────────────────┐
│  Pillow · PyMuPDF · FFmpeg · tiktoken · subprocess   │
│  FastAPI + uvicorn, bound to localhost only          │
└───────────────────────────┬──────────────────────────┘
                            │ websocket (127.0.0.1)
┌───────────────────────────▼──────────────────────────┐
│  WebView2 window   ── or ──   your browser tab       │
└──────────────────────────────────────────────────────┘
```

File conversion, extraction, hashing and packaging all execute in Python.
**Nothing is sandboxed by the browser, and both modes have identical system
capability.** Native mode differs only in presentation: a real application
window, no browser chrome, and access to OS file dialogs.

Native mode needs two things on Windows, both validated at startup with an
actionable message if either is absent:

| Requirement | Notes |
|---|---|
| `pywebview` | Installed by `requirements.txt`. Backs NiceGUI's `native=True`. |
| WebView2 Runtime | Preinstalled on Windows 11. Detected across both registry views, since EdgeUpdate registers itself in the 32-bit view. |

Only one instance may run at a time. The lock at `data/omniforge.lock` records
the owning PID and its start time, so a lock left behind by a crash is reclaimed
automatically on the next launch rather than locking you out.

---

## Core Kernel

The micro-kernel is feature-complete and covered by the project's test suite.

| Component | Responsibility |
|---|---|
| `core/registry.py` | Discovers `modules/**/manifest.json`, validates each manifest, gates it by version `tier`, imports the package, and calls `on_load()`. A module that fails any step is marked **DEGRADED** and surfaced in the sidebar — it never takes down the host. |
| `core/event_bus.py` | The sole inter-component channel. Async pub/sub; handlers run concurrently and one failing handler cannot suppress its siblings. |
| `core/base_module.py` | The contract every module implements: identity properties, `on_load`/`on_unload`, an `execute()` async generator, and `build_ui()`. |
| `core/sandbox.py` | Runs module work off the GUI event loop. Every module's `execute()` is driven through `SandboxTask.consume()`, so the 300s timeout (configurable) and the Cancel button in each module apply to real work — see `docs/rfcs/0003`. |
| `core/storage.py` | Thread-safe TinyDB wrapper — every read and write is lock-guarded. |
| `core/logger.py` | Human-readable console output plus one JSON object per line in `data/logs/omniforge.log`. Only allow-listed fields are serialised, so incidental data cannot leak into logs. |
| `core/recycle_store.py` | Destructive operations move files here first, restorable for 24 hours from the **Recycle Bin** in the header. Expired batches are purged at startup. Payloads stay on the source's own volume, so recycling never turns into a multi-gigabyte cross-drive copy. |
| `core/permission_manager.py` | The only sanctioned elevation path. Windows uses `ShellExecuteEx` RUNAS and reads the real exit code back; POSIX prefers `pkexec`, falling back to `sudo`. |
| `core/theme_engine.py` | Dark, Light and Cyberpunk palettes delivered as CSS custom properties, so switching repaints instantly without a restart. |
| `core/command_palette.py` | Ctrl+K fuzzy search across module names, descriptions, tags and recent operations. |
| `core/dependency_checker.py` | Validates platform prerequisites before the window opens (WebView2 on Windows). |

### Safety guarantees

- **Never writes outside its lane.** `shared/validators.validate_write_target()` confines module output to `exports/`, `temp/`, or a directory the user explicitly picked.
- **Never deletes without a way back.** Destructive operations route through the recycle store and show a computed impact summary (`"4.2 GB across 1,247 files"`) before proceeding.
- **Never elevates blindly.** An elevation request must carry a reason, and `describe()` renders the exact shell-quoted command for the user to read first.
- **Never phones home.** No network calls, telemetry, or analytics in any feature.

---

## Modules

### PDF Suite — `modules/converters/pdf_suite/`

Nine PDF operations, all running through PyMuPDF with no external binary, so
the whole suite works with no network and nothing installed beyond pip.

| Operation | Notes |
|---|---|
| Merge | Combines any number of PDFs; rows can be reordered before running |
| Split | Every N pages, one file per page, or a single page range |
| Compress | Screen / eBook / Print presets resample **only** embedded rasters — text and vectors stay sharp |
| Remove Password | Saves an unlocked copy of a document you can already open |
| Edit Metadata | Title, author, subject, keywords; untouched fields are preserved |
| Rotate | 90 / 180 / 270°, accumulating on already-rotated pages |
| Extract Text | Writes the text layer to UTF-8 |
| Extract Images | Saves embedded rasters into a per-document folder |
| Convert to DOCX | Editable Word output via pdf2docx |

Every operation accepts a password, so an encrypted source works throughout —
not just for unlocking. Results are written to `exports/pdf_suite/`.

### Image Suite — `modules/converters/image_suite/`

Seven operations covering raster work and SVG asset export.

| Operation | Notes |
|---|---|
| Convert | HEIC/HEIF, WebP, PNG, JPG, BMP, TIFF in; JPEG/PNG/WebP out. Transparency is flattened onto white only where the target format cannot carry alpha |
| Compress to size | Binary-searches encoder quality to land under a target KB, encoding into memory so only the chosen result is written |
| Resize | Caps the longest edge; smaller images are left alone rather than upscaled |
| Strip metadata | Removes EXIF, ICC and XMP by re-encoding through a fresh image |
| SVG → density PNGs | mdpi/hdpi/xhdpi/xxhdpi/xxxhdpi at 1x–4x |
| SVG → favicons | 8 standard sizes plus a multi-resolution `.ico` |
| SVG → VectorDrawable | Android XML; gradients and filters are reported, never silently dropped |

SVG rasterisation goes SVG → PDF → PNG through svglib and PyMuPDF. reportlab's
own raster backend needs the Cairo system library; this route uses only
self-contained wheels.

### Media Suite — `modules/converters/media_suite/`

Video and audio work via FFmpeg, which is looked up in `bundled/` before PATH
so a packaged build is self-contained.

| Operation | Notes |
|---|---|
| Compress video | Derives the bitrate from target size and duration, subtracting the audio budget, so the result lands near the limit |
| Extract audio | MP3 at 192k; a silent video is reported rather than producing an empty file |
| Convert to MP4 | From WebM, MKV, MOV, AVI, with `+faststart` for streaming |
| Thumbnails | Stills spread evenly across the clip |
| Normalise loudness | EBU R128: −16 LUFS integrated, −1.5 dB true peak |

Presets target Discord (10 MB), Email (25 MB) and Web (50 MB), or a custom size.
Every invocation is bounded by a timeout, so a wedged encode cannot hang the app.

**Without FFmpeg the module loads DEGRADED** with install instructions, rather
than failing when you press Run.

### Document Suite — `modules/converters/document_suite/`

Seven conversions between the formats developers move data through, all pure
Python — no Pandoc, no LaTeX, no system install.

| Conversion | Notes |
|---|---|
| Markdown → HTML | Self-contained page: styles and syntax highlighting are inlined, so it renders offline and in dark mode |
| HTML → Markdown | Round-trips back through the Markdown converter |
| JSON → CSV | Nested objects flatten to dotted columns (`user.city`), or stay as JSON text |
| JSON → Excel | `.xlsx` with a bold header row and auto-sized columns |
| CSV / TSV → JSON | Optional type inference; turn it off to keep `007` a string |
| JSON ↔ YAML | Key order preserved, Unicode intact, `safe_load` only |

Ragged JSON arrays keep every column, Excel's UTF-8 BOM is stripped on read,
and results are written to `exports/document_suite/`.

### Smart File Filter — `modules/extractors/file_filter/`

Scans a directory tree, reports what it holds, and extracts the extensions
you choose.

- Recursive scan with gitignore-style exclude patterns (`pathspec`); `node_modules`,
  `.git`, `__pycache__` and similar rebuildable trees are excluded by default
- Four output modes: **Copy**, **Move**, **Zip**, **Manifest** (a tab-separated
  listing that copies nothing)
- Directory hierarchy is preserved by default, or flattened into a single folder
- **Move is the only destructive mode** — originals route through
  `core/recycle_store` first, so an extraction is undoable for 24 hours (rule B-04)
- An output directory nested inside the source is rejected outright, so a run
  can never feed on its own output
- Results are written to `exports/file_filter/`

### LLM Context Packager — `modules/extractors/llm_packager/`

Packs a source tree into a single context file for pasting into an LLM prompt.
This is the V1 flagship.

- Recursive scan with gitignore-style exclude patterns (`pathspec`)
- Include-by-extension filtering; files over 5 MiB are skipped
- Per-file headers delimiting each file in the output
- Token counting via `tiktoken` (GPT-4o / GPT-4 / GPT-3.5 encodings)

  `tiktoken` fetches its BPE vocabulary over the internet the first time it is
  used. OmniForge never allows that (rule C-01): it checks for an existing
  local cache and, when none is present, estimates the token count from
  character length and labels the figure as approximate.
- Folder browser that walks the machine's filesystem, so it works the same in
  the native window and in a browser tab
- Last-used directory, extensions, excludes and model persisted between runs
- Output written to `exports/llm_context_<timestamp>.txt`

### Bulk Regex Renamer — `modules/extractors/bulk_renamer/`

Renames many files at once with a regular expression, previewed before
anything changes.

- The pattern matches each file's **stem** (name without extension) — the
  extension is always preserved, so a careless global pattern can't mangle it
- Replacement templates support regex backreferences (`\1`, `\g<name>`), a
  per-match counter (`{n}` / `{n:03d}`), and today's date (`{date}`)
- A live preview (ag-grid) shows original → proposed → status for every file
  before anything runs; **Run re-derives from that exact same computation**,
  so what was previewed is exactly what executes
- A rename that would collide with another file's new name, or with a
  different existing file, is skipped and reported rather than overwriting
  anything — a case-only rename on a case-insensitive filesystem is correctly
  recognised as safe, not a collision
- Renaming isn't destructive — nothing is removed from disk — so a completed
  run can be reversed with one click via **Undo**, which simply renames each
  file back

### Duplicate Detective — `modules/extractors/duplicate_finder/`

Finds files with byte-identical content and reclaims the wasted space.

- Two-pass detection: files are grouped by size first (cheap), then only
  files sharing a size with at least one other file are hashed (xxh3_128) —
  a same-size, different-content collision is never merged into a group
- Hashing runs across a shared process pool once there are enough candidates
  to justify it (pure CPU work gets no benefit from threads under the GIL);
  falls back to sequential hashing in-thread if the pool itself is broken
- Three keep strategies: newest, oldest, or a manual per-group choice
- Each group can be individually excluded from a run
- **Deletion always keeps exactly one file per group** — the rest route
  through `core/recycle_store` first, so a resolve run is undoable for 24
  hours (rule B-04)
- Zero-byte files are excluded by default (they reclaim nothing and would
  otherwise form one meaningless group); configurable via minimum file size

---

## Development

```bash
make check
```

Runs the full gate: `ruff`, `mypy --strict`, and the test suite.

| Command | Purpose |
|---|---|
| `make run` | Launch the application |
| `make lint` | `ruff check --fix` and `ruff format` |
| `make typecheck` | `mypy --strict .` |
| `make test` | pytest with coverage |

### Project layout

```
core/       Micro-kernel — registry, event bus, sandbox, storage, logging, safety services
shared/     Leaf-layer utilities — constants, validators, formatters, file and UI helpers
ui/         Application shell — header, sidebar, content area
modules/    V1 feature modules, one directory per module, grouped by pillar
_deferred/  Parked V2/V3 modules (git-ignored, excluded from tooling) — not shipped in V1
```

The dependency order is strict: `shared` depends on nothing internal, `core` may use `shared`, and modules may use both but never each other — they communicate exclusively over the EventBus. These rules are enforced by executable checks in `core/tests/test_architecture.py`, so a violation fails the test suite rather than review.

### Adding a module

A module directory contains `__init__.py` (exposing a `create()` factory), `manifest.json`, `logic.py`, `ui.py`, `models.py`, `constants.py`, and `tests/`. The manifest must declare a `tier` (`v1`/`v2`/`v3`); only shipped tiers load. `logic.py` holds all business logic and imports no NiceGUI; `ui.py` holds all presentation and reaches logic only through the EventBus. The registry discovers the module automatically on next launch.
