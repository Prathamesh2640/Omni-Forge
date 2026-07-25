.PHONY: run test lint typecheck check install install-dev clean build

# ─── Development ──────────────────────────────────────────────────────────────
run:
	python app.py

# ─── Quality ──────────────────────────────────────────────────────────────────
lint:
	ruff check . --fix
	ruff format .

typecheck:
	mypy --strict .

check: lint typecheck test

# ─── Testing ──────────────────────────────────────────────────────────────────
test:
	pytest --cov --cov-report=term-missing

test-verbose:
	pytest -v --cov --cov-report=term-missing

# ─── Install ──────────────────────────────────────────────────────────────────
install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements.txt -r requirements-dev.txt
	pre-commit install

# ─── Build ────────────────────────────────────────────────────────────────────
# omniforge.spec is a Phase 8 deliverable. Say so plainly rather than letting
# PyInstaller fail with a stack trace about a missing file.
build:
	@python -c "import pathlib, sys; sys.exit(0) if pathlib.Path('omniforge.spec').is_file() else (print('omniforge.spec does not exist yet - packaging is Phase 8.'), sys.exit(1))"
	pyinstaller omniforge.spec

# ─── Clean ────────────────────────────────────────────────────────────────────
# Cross-platform: this project is Windows-primary, where find/rm are absent.
clean:
	python -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
	python -c "import pathlib; [p.unlink() for p in pathlib.Path('.').rglob('*.pyc')]"
	python -c "import shutil, pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ['.mypy_cache', '.ruff_cache', '.pytest_cache', 'htmlcov']]; pathlib.Path('.coverage').unlink(missing_ok=True)"
