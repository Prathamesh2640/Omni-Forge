# OmniForge — Setup & Development Guide

Everything you need to install, run, test, and build OmniForge.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11+ | Strictly required. 3.12 also supported. |
| Git | Any | For version control |
| Make | Any | Windows: install via [Chocolatey](https://chocolatey.org/) — `choco install make` |
| FFmpeg | Latest | Optional at dev time — required for media modules |
| Tesseract | 5.x | Optional at dev time — required for OCR modules |

**Windows only:**
- [WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) — required for `nicegui native=True`. Already installed on Windows 11 and most updated Windows 10 machines.
- [Npcap](https://npcap.com/) — required for network scanning module (scapy ARP). Install with "WinPcap API-compatible mode" checked.

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
> uv pip install -r requirements.txt
> uv pip install -r requirements-dev.txt
> ```

### 3. Install pre-commit hooks

```bash
pre-commit install
```

This installs git hooks that run ruff, mypy, and a quick pytest pass before every commit. They run automatically — you do not need to invoke them manually.

---

## Running the App

### Development mode (recommended)

```bash
# Using Make
make run

# Direct
python app.py --dev
```

`--dev` enables:
- Verbose logging to console
- EventBus message trace in a dedicated console tab
- Hot-reload of module UI files on save

The app opens in a native window on port 8765. Do not open a browser — the native window handles everything.

### Production mode

```bash
python app.py
```

---

## Running Tests

```bash
# Full test suite
make test

# Direct
pytest tests/ -v --cov=. --cov-report=html

# Single module
make test-module MODULE=converters.pdf_suite

# Quick run (no coverage report)
pytest tests/ -v
```

After a coverage run, open `htmlcov/index.html` in a browser to view the report.

---

## Linting & Type Checking

```bash
# Run both (what CI checks)
make lint

# Individually
ruff check .
mypy --strict .

# Auto-fix ruff violations where possible
ruff check . --fix
```

The project enforces **zero tolerance** — both must pass clean before any commit is accepted by the pre-commit hook.

---

## All Make Commands

| Command | What It Does |
|---|---|
| `make run` | Start app in development mode |
| `make test` | Run full test suite with coverage |
| `make test-module MODULE=<pillar.name>` | Run tests for one module |
| `make lint` | Run ruff + mypy |
| `make build-windows` | PyInstaller Windows build |
| `make build-linux` | PyInstaller Linux AppImage |
| `make build-macos` | PyInstaller macOS .dmg |
| `make clean` | Remove build/, dist/, __pycache__, .coverage |

---

## External Binary Setup (Dev Environment)

The modules that wrap external binaries (FFmpeg, Tesseract, ADB) will mark themselves `DEGRADED` at startup if the binary is not found. This is expected during early development. You only need these when working on the relevant module.

### FFmpeg

```bash
# Windows (Chocolatey)
choco install ffmpeg

# Linux
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

### Tesseract

```bash
# Windows — download installer from:
# https://github.com/UB-Mannheim/tesseract/wiki

# Linux
sudo apt install tesseract-ocr

# macOS
brew install tesseract
```

### ADB (Android Debug Bridge)

```bash
# Windows (Chocolatey)
choco install adb

# Linux
sudo apt install adb

# macOS
brew install android-platform-tools
```

Verify ADB is accessible:
```bash
adb version
```

---

## How to Test a Feature in the GUI

After implementing a module:

1. Start the app: `make run`
2. The module should appear in the sidebar under its pillar group.
3. If it shows a warning icon, it is in `DEGRADED` state — check the console log for the reason (usually a missing binary or failed `on_load()`).
4. Test each sub-feature end-to-end using sample files from `modules/<pillar>/<module>/tests/fixtures/`.
5. Verify the progress bar updates during long operations.
6. Verify the output file is written to `exports/` (or your selected directory).
7. For any destructive operation: verify the confirmation dialog appears with an accurate impact summary.
8. Verify undo works (file should appear in `data/recycle/`).
9. Check `[ ]` items in `FEATURES.md` as you confirm them.

---

## Project Structure (Quick Reference)

```
OmniForge/
├── app.py                  # Entry point
├── pyproject.toml          # Project config, ruff, mypy settings
├── requirements.txt        # Production deps
├── requirements-dev.txt    # Dev/test deps
├── Makefile                # Dev shortcuts
├── SETUP.md                # This file
│
├── core/                   # Kernel — do not modify without an RFC
│   ├── base_module.py
│   ├── module_registry.py
│   ├── event_bus.py
│   ├── sandbox.py
│   ├── storage.py
│   ├── permission_manager.py
│   ├── theme_engine.py
│   ├── logger.py
│   ├── recycle_store.py
│   ├── command_palette.py
│   └── dependency_checker.py
│
├── shared/                 # Utilities importable by all modules
│   ├── constants.py
│   ├── validators.py
│   ├── formatters.py
│   ├── file_utils.py
│   └── ui_components.py
│
├── modules/                # All plugin modules — 8 pillars
│   ├── converters/         # Pillar 1: Document & Media Factory
│   ├── extractors/         # Pillar 2: Source & Extraction Engine
│   ├── android/            # Pillar 3: Android Workshop
│   ├── languages/          # Pillar 4: Language Lab
│   ├── devops/             # Pillar 5: Infra & Cloud Cockpit
│   ├── system_matrix/      # Pillar 6: System Matrix
│   ├── network_vault/      # Pillar 7: Network & Security Vault
│   └── automation/         # Pillar 8: Servant Automation
│
├── assets/                 # Icons, themes, fonts, splash
├── bundled/                # Platform-specific external binaries (gitignored)
├── exports/                # Default user output directory (gitignored)
├── temp/                   # Ephemeral files (gitignored)
├── data/                   # TinyDB stores, logs, recycle (gitignored)
└── tests/                  # Integration & system tests
```

---

## Adding a New Module

1. Create the directory: `modules/<pillar>/<module_name>/`
2. Add `manifest.json`, `models.py`, `logic.py`, `ui.py`, `constants.py`, `__init__.py`
3. Add `tests/` with `test_logic.py`, `test_ui.py`, `fixtures/`
4. Run `make test-module MODULE=<pillar>.<module_name>`
5. Run `make run` and verify the module appears in the sidebar
6. The Module Registry picks it up automatically — no registration needed

See `docs/module_guide.md` for the complete step-by-step authoring guide.

---

## Common Issues

**App window does not open (Windows)**
Install WebView2 Runtime from [Microsoft](https://developer.microsoft.com/en-us/microsoft-edge/webview2/).

**Module shows warning icon on startup**
Check the console log. Common causes: missing pip package, missing system binary (FFmpeg/Tesseract/ADB), failed `on_load()`.

**`ruff` or `mypy` fails after installing dependencies**
Run `pre-commit run --all-files` to catch and auto-fix what it can. Remaining issues must be fixed manually — the project enforces strict mode.

**Port 8765 already in use**
Another OmniForge instance is running, or the previous run did not exit cleanly. Kill the existing process, then delete `data/omniforge.lock` if it exists.

**`scapy` ARP scan fails on Windows**
Install [Npcap](https://npcap.com/) with "WinPcap API-compatible mode" enabled.

**`rembg` downloads a large model on first use**
This is expected. The U2Net ONNX model (~170 MB) downloads once and is cached. The module's `on_load()` will prompt you before initiating the download.
