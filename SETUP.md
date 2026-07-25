# OmniForge — Setup & Development Guide

Everything you need to install, run, test, and build OmniForge V1.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11+ | Strictly required. 3.12 also supported. |
| Git | Any | For version control |
| Make | Any | Optional. Windows: `choco install make`. Every target is a plain command you can also run directly. |
| FFmpeg | Latest | Optional at dev time — required only for the Media Suite. |

**Windows only:**
- [WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) — required for `nicegui native=True`. Already installed on Windows 11 and most updated Windows 10 machines.

---

## First-Time Setup

### 1. Create and activate a virtual environment

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux / macOS
python3.11 -m venv .venv
source .venv/bin/activate
```

> All subsequent commands assume the virtual environment is active.

### 2. Install dependencies

```bash
# Production dependencies
pip install -r requirements.txt

# Development dependencies (linting, testing, type checking)
pip install -r requirements-dev.txt
```

> **Faster alternative:** Use [uv](https://github.com/astral-sh/uv) — a drop-in replacement for pip that is significantly faster.
> ```bash
> pip install uv
> uv pip install -r requirements.txt -r requirements-dev.txt
> ```

### 3. Install pre-commit hooks

```bash
pre-commit install
```

This installs git hooks that run ruff and mypy before every commit.

---

## Running the App

```bash
# Native desktop window (default)
python app.py        # or: make run

# Browser tab instead — useful for debugging, CI, or a box without WebView2
python app.py --browser
```

The app binds to `127.0.0.1:8765`. Only one instance may run at a time; the lock
at `data/omniforge.lock` is reclaimed automatically if a previous run crashed.

---

## Running Tests

```bash
# Full suite with coverage (what CI runs)
make test            # or: pytest --cov --cov-report=term-missing

# Verbose
make test-verbose    # or: pytest -v --cov --cov-report=term-missing

# A single module
pytest modules/converters/pdf_suite -q
```

Test discovery is configured in `pyproject.toml` (`testpaths = core, shared,
modules`), so a bare `pytest` finds everything. Tests live beside the code they
cover, in each package's `tests/` directory.

---

## Linting & Type Checking

```bash
make lint            # ruff check . --fix  +  ruff format .
make typecheck       # mypy --strict .
make check           # lint + typecheck + test (the full gate)
```

The project enforces **zero tolerance** — ruff and mypy `--strict` must both pass
clean. The parked `_deferred/` tree is excluded from all three tools.

---

## All Make Commands

| Command | What It Does |
|---|---|
| `make run` | Start the app (native window) |
| `make lint` | `ruff check --fix` + `ruff format` |
| `make typecheck` | `mypy --strict .` |
| `make test` | Full test suite with coverage |
| `make test-verbose` | Test suite, verbose |
| `make check` | lint + typecheck + test |
| `make install` | Install production dependencies |
| `make install-dev` | Install dev dependencies + pre-commit hooks |
| `make build` | PyInstaller build (requires `omniforge.spec`, a packaging deliverable) |
| `make clean` | Remove caches and coverage artefacts |

---

## External Binary Setup (Dev Environment)

The Media Suite wraps the FFmpeg binary and marks itself `DEGRADED` at startup
if FFmpeg is not found (rule B-06) — everything else works without it. You only
need it when working on `media_suite`.

### FFmpeg

```bash
# Windows (Chocolatey)
choco install ffmpeg

# Linux
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

---

## How to Test a Feature in the GUI

After implementing a module:

1. Start the app: `make run`
2. The module should appear in the sidebar under its pillar group (Converters or Extractors).
3. If it shows a warning icon, it is in `DEGRADED` state — check the console log for the reason (usually a missing binary or a failed `on_load()`).
4. Test each sub-feature end-to-end using sample files from `modules/<pillar>/<module>/tests/fixtures/`.
5. Verify the progress bar updates during long operations.
6. Verify the output file is written to `exports/` (or your selected directory).
7. For any destructive operation: verify the confirmation dialog appears with an accurate impact summary.
8. Verify undo works (file should appear in `data/recycle/`).

---

## Project Structure (Quick Reference)

```
OmniForge/
├── app.py                  # Entry point
├── pyproject.toml          # Project config, ruff, mypy, pytest settings
├── requirements.txt        # V1 production deps
├── requirements-optional.txt  # Parked-tier / deferred-feature deps
├── requirements-dev.txt    # Dev/test deps
├── Makefile                # Dev shortcuts
├── SETUP.md                # This file
│
├── core/                   # Kernel — do not modify without an RFC (docs/rfcs/)
│   ├── base_module.py
│   ├── registry.py
│   ├── event_bus.py
│   ├── sandbox.py
│   ├── storage.py
│   ├── permission_manager.py
│   ├── theme_engine.py
│   ├── logger.py
│   ├── recycle_store.py
│   ├── single_instance.py
│   ├── command_palette.py
│   └── dependency_checker.py
│
├── shared/                 # Utilities importable by all modules
│   ├── constants.py
│   ├── validators.py
│   ├── formatters.py
│   ├── file_utils.py
│   ├── platform_info.py
│   ├── process_guard.py
│   └── ui_components.py
│
├── ui/                     # Application shell — header, sidebar, content area
│   └── shell.py
│
├── modules/                # V1 plugin modules — 2 pillars
│   ├── converters/         # Pillar 1: pdf_suite, image_suite, media_suite, document_suite
│   └── extractors/         # Pillar 2: llm_packager, file_filter, duplicate_finder, bulk_renamer
│
├── _deferred/              # Parked V2/V3 modules (gitignored, excluded from tooling)
├── docs/rfcs/              # Architecture decision records
├── assets/                 # Icons, themes, fonts
├── bundled/                # Platform-specific external binaries (gitignored)
├── exports/                # Default user output directory (gitignored)
├── temp/                   # Ephemeral files (gitignored)
└── data/                   # TinyDB stores, logs, recycle, lock (gitignored)
```

---

## Adding a New Module

1. Create the directory: `modules/<pillar>/<module_name>/`
2. Add `manifest.json` (including a `tier` field), `models.py`, `logic.py`, `ui.py`, `constants.py`, `__init__.py`
3. Add `tests/` with `test_logic.py`, `test_ui.py`, `fixtures/`
4. Run `pytest modules/<pillar>/<module_name> -q`
5. Run `make run` and verify the module appears in the sidebar
6. The Registry picks it up automatically — no registration needed. Only modules whose `tier` is in `shared.constants.SHIPPED_TIERS` load.

---

## Common Issues

**App window does not open (Windows)**
Install WebView2 Runtime from [Microsoft](https://developer.microsoft.com/en-us/microsoft-edge/webview2/).

**Module shows warning icon on startup**
Check the console log. Common causes: missing pip package, missing system binary (FFmpeg), or a failed `on_load()`.

**`ruff` or `mypy` fails after installing dependencies**
Run `pre-commit run --all-files` to catch and auto-fix what it can. Remaining issues must be fixed manually — the project enforces strict mode.

**Port 8765 already in use**
Another OmniForge instance is running, or the previous run did not exit cleanly. Kill the existing process, then delete `data/omniforge.lock` if it remains.
