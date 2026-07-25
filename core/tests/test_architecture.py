"""Executable checks for the OmniForge architectural laws.

These enforce the layering and code-quality rules that a linter cannot see.
A failure here means a structural rule was broken, not that a feature is
wrong — read the rule id in the assertion message.
"""
from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared.constants import SHIPPED_TIERS

_ROOT = Path(__file__).resolve().parents[2]
_LAYERS = ("core", "shared", "ui", "modules")

_REQUIRED_MANIFEST_KEYS = frozenset(
    {
        "name", "id", "version", "description", "author", "pillar", "tier",
        "icon", "tags", "min_python_version", "requires_elevation",
        "external_binaries", "pip_dependencies", "disabled_on", "experimental",
    }
)


def _source_files(layer: str) -> Iterator[Path]:
    """Yield non-test Python files within *layer*."""
    for path in (_ROOT / layer).rglob("*.py"):
        if "tests" not in path.parts:
            yield path


def _imported_roots(path: Path) -> set[str]:
    """Return the top-level package names *path* imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _module_packages() -> list[Path]:
    """Return every directory under modules/ that contains a manifest."""
    return [manifest.parent for manifest in (_ROOT / "modules").rglob("manifest.json")]


def _all_source_files() -> list[Path]:
    """Return every non-test source file across all layers."""
    return [path for layer in _LAYERS for path in _source_files(layer)]


# ─── A — Structural integrity ─────────────────────────────────────────────────


@pytest.mark.parametrize("path", list(_source_files("core")), ids=str)
def test_core_never_imports_modules(path: Path) -> None:
    """Rule A-07: core/ must not depend on modules/."""
    assert "modules" not in _imported_roots(path), f"A-07: {path.name} imports modules/"


@pytest.mark.parametrize("path", list(_source_files("shared")), ids=str)
def test_shared_is_the_lowest_layer(path: Path) -> None:
    """Rule A-07: shared/ must not depend on core/, ui/ or modules/."""
    forbidden = _imported_roots(path) & {"core", "ui", "modules"}
    assert not forbidden, f"A-07: {path.name} imports {sorted(forbidden)}"


def test_no_module_imports_another_module() -> None:
    """Rule A-03: modules communicate only via the EventBus."""
    violations: list[str] = []
    for package in _module_packages():
        own_prefix = ".".join(package.relative_to(_ROOT).parts)
        for path in package.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if node.module.startswith("modules.") and not node.module.startswith(
                    own_prefix
                ):
                    violations.append(f"{path.name} imports {node.module}")
    assert not violations, f"A-03: {violations}"


@pytest.mark.parametrize("package", _module_packages(), ids=lambda p: p.name)
def test_module_has_the_required_files(package: Path) -> None:
    """Rule A-04: every module ships logic, ui, models, constants and tests."""
    for required in ("logic.py", "ui.py", "models.py", "constants.py", "__init__.py"):
        assert (package / required).is_file(), f"A-04: {package.name} lacks {required}"
    assert (package / "tests").is_dir(), f"A-04: {package.name} lacks tests/"


@pytest.mark.parametrize("package", _module_packages(), ids=lambda p: p.name)
def test_module_exposes_a_create_factory(package: Path) -> None:
    """Rule A-02: the Registry instantiates modules through create()."""
    source = (package / "__init__.py").read_text(encoding="utf-8")
    assert "def create(" in source, f"A-02: {package.name} has no create() factory"


@pytest.mark.parametrize("package", _module_packages(), ids=lambda p: p.name)
def test_manifest_declares_every_required_field(package: Path) -> None:
    """Rule A-05: manifests must be complete or the Registry degrades them."""
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    missing = _REQUIRED_MANIFEST_KEYS - manifest.keys()
    assert not missing, f"A-05: {package.name} manifest missing {sorted(missing)}"


@pytest.mark.parametrize("package", _module_packages(), ids=lambda p: str(p.name))
def test_manifest_icon_exists_on_disk(package: Path) -> None:
    """A declared icon that is not there is a silent lie.

    Nothing loads the icon at runtime today, so five modules shipped pointing at
    SVGs that did not exist and nobody noticed. The moment the sidebar renders
    them it would be five broken images.
    """
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    icon = _ROOT / str(manifest["icon"])
    assert icon.is_file(), f"{package.name}: manifest icon not found at {icon}"


@pytest.mark.parametrize("package", _module_packages(), ids=lambda p: p.name)
def test_manifest_id_matches_its_directory(package: Path) -> None:
    """A mismatch makes the module unreachable from the sidebar and palette."""
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    expected = f"{package.parent.name}.{package.name}"
    assert manifest["id"] == expected, f"A-05: expected id {expected!r}"


@pytest.mark.parametrize("package", _module_packages(), ids=lambda p: p.name)
def test_module_in_tree_is_the_shipped_tier(package: Path) -> None:
    """RFC 0007: the V1 tree contains only shipped-tier modules.

    Parked tiers live under ``_deferred/``; a V2/V3 module dropped back into
    ``modules/`` would be gated out at runtime, but this makes the boundary a
    build-time guarantee rather than a silent skip nobody sees.
    """
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tier"] in SHIPPED_TIERS, (
        f"{package.name}: tier {manifest['tier']!r} is not shipped "
        f"(SHIPPED_TIERS={sorted(SHIPPED_TIERS)}); move it under _deferred/"
    )


@pytest.mark.parametrize("package", _module_packages(), ids=lambda p: p.name)
def test_logic_layer_is_free_of_nicegui(package: Path) -> None:
    """Rule A-01: business logic must carry no presentation dependency."""
    assert "nicegui" not in _imported_roots(package / "logic.py"), (
        f"A-01: {package.name}/logic.py imports nicegui"
    )


# ─── D — Code quality ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", _all_source_files(), ids=str)
def test_no_print_statements(path: Path) -> None:
    """Rule D-06: all output goes through core.logger."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert not offenders, f"D-06: print() at {path.name}:{offenders}"


@pytest.mark.parametrize("path", _all_source_files(), ids=str)
def test_no_bare_except(path: Path) -> None:
    """Rule D-07: every caught exception must be typed and logged."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]
    assert not offenders, f"D-07: bare except at {path.name}:{offenders}"


@pytest.mark.parametrize("path", _all_source_files(), ids=str)
def test_public_functions_have_docstrings(path: Path) -> None:
    """Rule D-04: every public function and class is documented."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    undocumented = [
        f"{node.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
        and ast.get_docstring(node) is None
    ]
    assert not undocumented, f"D-04: undocumented in {path.name}: {undocumented}"


# ─── B — Process safety ───────────────────────────────────────────────────────


def _writing_module_packages() -> list[Path]:
    """Return module packages whose logic writes to a caller-supplied directory.

    Detected by the presence of an ``output_dir`` field on the module's params —
    a module that never receives a destination has nothing to confine.
    """
    packages: list[Path] = []
    for package in _module_packages():
        models = package / "models.py"
        if models.is_file() and "output_dir" in models.read_text(encoding="utf-8"):
            packages.append(package)
    return packages


@pytest.mark.parametrize("package", _writing_module_packages(), ids=lambda p: p.name)
def test_writing_modules_confine_their_output(package: Path) -> None:
    """Rule B-07: writes go only to exports/, temp/, or the user's choice.

    This rule had no executable check, and six of the nine writing modules were
    not calling the validator at all — they wrote wherever ``output_dir``
    pointed (audit §3.8).
    """
    source = (package / "logic.py").read_text(encoding="utf-8")
    assert "validate_write_target" in source, (
        f"B-07: {package.name}/logic.py accepts an output_dir but never passes "
        "it through shared.validators.validate_write_target"
    )
