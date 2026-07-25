"""Unit tests for core.registry (manifest validation and degraded handling)."""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from core import registry as registry_mod
from core.base_module import BaseModule
from core.models import ProgressEvent
from core.registry import _Registry

_VALID_MANIFEST: dict[str, Any] = {
    "name": "Demo Module",
    "id": "demo.sample",
    "version": "1.0.0",
    "description": "A module used only by the registry tests.",
    "author": "OmniForge Team",
    "pillar": "demo",
    "tier": "v1",
    "icon": "assets/icons/demo.svg",
    "tags": ["demo"],
    "min_python_version": "3.11",
    "requires_elevation": False,
    "external_binaries": [],
    "pip_dependencies": [],
    "disabled_on": [],
    "experimental": False,
}


class _StubModule(BaseModule):
    """Minimal BaseModule used to verify Registry wiring."""

    def __init__(self, fail_on_load: bool = False) -> None:
        self.fail_on_load = fail_on_load
        self.unloaded = False

    @property
    def module_id(self) -> str:
        return "demo.sample"

    @property
    def name(self) -> str:
        return "Demo Module"

    @property
    def pillar(self) -> str:
        return "demo"

    @property
    def icon(self) -> str:
        return "assets/icons/demo.svg"

    async def on_load(self) -> None:
        if self.fail_on_load:
            raise RuntimeError("ffmpeg not found on PATH")

    async def on_unload(self) -> None:
        self.unloaded = True

    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        yield ProgressEvent(percent=100, message="done")

    def build_ui(self, container: Any) -> None:
        return None


@pytest.fixture
def modules_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the Registry at a temporary modules/ tree."""
    root = tmp_path / "modules"
    root.mkdir()
    monkeypatch.setattr(registry_mod, "_MODULES_ROOT", root)
    return root


def _write_manifest(root: Path, **overrides: Any) -> Path:
    """Write a manifest for the demo module, applying *overrides*."""
    manifest = {**_VALID_MANIFEST, **overrides}
    pkg = root / "demo" / "sample"
    pkg.mkdir(parents=True, exist_ok=True)
    path = pkg / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.fixture
def stub_import(monkeypatch: pytest.MonkeyPatch) -> list[_StubModule]:
    """Patch importlib so the Registry receives a _StubModule instead of real code."""
    created: list[_StubModule] = []

    class _FakePackage:
        @staticmethod
        def create() -> _StubModule:
            module = _StubModule()
            created.append(module)
            return module

    monkeypatch.setattr(
        registry_mod.importlib, "import_module", lambda _path: _FakePackage
    )
    return created


# ─── Happy path ───────────────────────────────────────────────────────────────


async def test_loads_a_valid_module(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    _write_manifest(modules_root)
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.get("demo.sample") is not None
    assert reg.degraded_modules() == {}


async def test_all_modules_lists_the_loaded_instance(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    _write_manifest(modules_root)
    reg = _Registry()
    await reg.discover_and_load()

    assert [m.module_id for m in reg.all_modules()] == ["demo.sample"]


async def test_manifest_metadata_is_retained_for_search(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    """The command palette indexes description and tags, which BaseModule lacks."""
    _write_manifest(modules_root, tags=["alpha", "beta"])
    reg = _Registry()
    await reg.discover_and_load()

    metadata = reg.all_metadata()

    assert len(metadata) == 1
    assert metadata[0].description == "A module used only by the registry tests."
    assert metadata[0].tags == ["alpha", "beta"]
    assert metadata[0].experimental is False


async def test_malformed_tags_do_not_break_metadata(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    _write_manifest(modules_root, tags="not-a-list")
    reg = _Registry()
    await reg.discover_and_load()

    assert reg.all_metadata()[0].tags == []


async def test_degraded_modules_contribute_no_metadata(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    _write_manifest(modules_root, min_python_version="99.0")
    reg = _Registry()
    await reg.discover_and_load()

    assert reg.all_metadata() == []


async def test_unload_clears_metadata(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    _write_manifest(modules_root)
    reg = _Registry()
    await reg.discover_and_load()

    await reg.unload_all()

    assert reg.all_metadata() == []


async def test_discovery_is_idempotent(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    """A second scan must not re-import or duplicate an already-loaded module."""
    _write_manifest(modules_root)
    reg = _Registry()

    await reg.discover_and_load()
    await reg.discover_and_load()

    assert len(reg.all_modules()) == 1
    assert len(stub_import) == 1


# ─── Degraded paths (rule B-06 — never crash the host) ────────────────────────


async def test_manifest_missing_required_keys_is_degraded(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    path = _write_manifest(modules_root)
    incomplete = {"id": "demo.sample", "name": "Demo Module"}
    path.write_text(json.dumps(incomplete), encoding="utf-8")
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.get("demo.sample") is None
    assert "manifest missing keys" in reg.degraded_modules()["demo.sample"]


async def test_malformed_json_is_degraded(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    path = _write_manifest(modules_root)
    path.write_text("{ not valid json", encoding="utf-8")
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.all_modules() == []
    assert reg.degraded_modules() != {}


async def test_future_python_requirement_is_degraded(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    _write_manifest(modules_root, min_python_version="99.0")
    reg = _Registry()

    await reg.discover_and_load()

    assert "requires Python 99.0" in reg.degraded_modules()["demo.sample"]


async def test_package_without_create_factory_is_degraded(
    modules_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(modules_root)

    class _NoFactory:
        pass

    monkeypatch.setattr(
        registry_mod.importlib, "import_module", lambda _path: _NoFactory
    )
    reg = _Registry()

    await reg.discover_and_load()

    assert "missing create()" in reg.degraded_modules()["demo.sample"]


async def test_on_load_failure_is_degraded_with_its_reason(
    modules_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing external binary must degrade the module, not crash the app."""
    _write_manifest(modules_root)

    class _FailingPackage:
        @staticmethod
        def create() -> _StubModule:
            return _StubModule(fail_on_load=True)

    monkeypatch.setattr(
        registry_mod.importlib, "import_module", lambda _path: _FailingPackage
    )
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.degraded_modules()["demo.sample"] == "ffmpeg not found on PATH"


async def test_degraded_module_is_not_retried_on_rescan(
    modules_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(modules_root)
    attempts: list[str] = []

    class _FailingPackage:
        @staticmethod
        def create() -> _StubModule:
            attempts.append("x")
            return _StubModule(fail_on_load=True)

    monkeypatch.setattr(
        registry_mod.importlib, "import_module", lambda _path: _FailingPackage
    )
    reg = _Registry()

    await reg.discover_and_load()
    await reg.discover_and_load()

    assert len(attempts) == 1


# ─── Platform exclusion ───────────────────────────────────────────────────────


async def test_module_disabled_on_this_platform_is_skipped_silently(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    """Platform exclusion is a clean skip — neither loaded nor degraded."""
    _write_manifest(modules_root, disabled_on=[sys.platform])
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.all_modules() == []
    assert reg.degraded_modules() == {}


async def test_module_disabled_on_another_platform_still_loads(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    other = "linux" if sys.platform == "win32" else "win32"
    _write_manifest(modules_root, disabled_on=[other])
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.get("demo.sample") is not None


async def test_non_list_disabled_on_is_tolerated(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    """A malformed disabled_on must not raise — it is treated as empty."""
    _write_manifest(modules_root, disabled_on="win32")
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.get("demo.sample") is not None


# ─── Version tier gate (RFC 0007) ─────────────────────────────────────────────


async def test_module_of_an_unshipped_tier_is_skipped_silently(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    """A parked tier is a clean skip — neither loaded nor degraded, and never
    imported (so its dependencies are never touched)."""
    _write_manifest(modules_root, tier="v2")
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.all_modules() == []
    assert reg.degraded_modules() == {}
    assert stub_import == []  # the package was never imported


async def test_module_with_an_unknown_tier_is_degraded(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    """An unrecognised tier is a manifest error, not a silent skip."""
    _write_manifest(modules_root, tier="v9")
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.get("demo.sample") is None
    assert "tier" in reg.degraded_modules()["demo.sample"]


async def test_manifest_without_a_tier_is_degraded(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    """``tier`` is a required manifest key — its absence degrades the module."""
    manifest = {k: v for k, v in _VALID_MANIFEST.items() if k != "tier"}
    pkg = modules_root / "demo" / "sample"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.get("demo.sample") is None
    assert "tier" in reg.degraded_modules()["demo.sample"]


# ─── Lifecycle ────────────────────────────────────────────────────────────────


async def test_missing_modules_root_is_handled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_mod, "_MODULES_ROOT", tmp_path / "does_not_exist")
    reg = _Registry()

    await reg.discover_and_load()

    assert reg.all_modules() == []


async def test_unload_all_calls_on_unload_and_clears(
    modules_root: Path, stub_import: list[_StubModule]
) -> None:
    _write_manifest(modules_root)
    reg = _Registry()
    await reg.discover_and_load()

    await reg.unload_all()

    assert stub_import[0].unloaded is True
    assert reg.all_modules() == []


async def test_unload_error_does_not_abort_the_shutdown(
    modules_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_manifest(modules_root)

    class _BadUnload(_StubModule):
        async def on_unload(self) -> None:
            raise RuntimeError("cleanup failed")

    class _Package:
        @staticmethod
        def create() -> _StubModule:
            return _BadUnload()

    monkeypatch.setattr(registry_mod.importlib, "import_module", lambda _path: _Package)
    reg = _Registry()
    await reg.discover_and_load()

    await reg.unload_all()

    assert reg.all_modules() == []


# ─── §3.12b/c — manifest parsing robustness ──────────────────────────────────


class TestPythonVersionParsing:
    """A declared patch level used to fail every module that had one.

    ``"3.11.0"`` built a 3-tuple compared against ``sys.version_info[:2]``,
    which can never be greater-or-equal, so such a module was always degraded.
    """

    @pytest.mark.parametrize("declared", ["3.11", "3.11.0", "3.9.7", "3"])
    async def test_a_satisfied_version_loads(
        self, modules_root: Path, stub_import: list[_StubModule], declared: str
    ) -> None:
        _write_manifest(modules_root, min_python_version=declared)
        registry = _Registry()

        await registry.discover_and_load()

        assert registry.get("demo.sample") is not None, registry.degraded_modules()

    async def test_an_unsatisfiable_version_degrades(
        self, modules_root: Path, stub_import: list[_StubModule]
    ) -> None:
        _write_manifest(modules_root, min_python_version="99.0")
        registry = _Registry()

        await registry.discover_and_load()

        assert "demo.sample" in registry.degraded_modules()

    async def test_a_nonsense_version_degrades_with_a_clear_reason(
        self, modules_root: Path, stub_import: list[_StubModule]
    ) -> None:
        """It used to raise ValueError from int() rather than report."""
        _write_manifest(modules_root, min_python_version="three-point-eleven")
        registry = _Registry()

        await registry.discover_and_load()

        reason = registry.degraded_modules().get("demo.sample", "")
        assert "not a version number" in reason


class TestManifestPeek:
    async def test_an_unparseable_manifest_still_degrades_visibly(
        self, modules_root: Path
    ) -> None:
        """Peek failures used to be swallowed silently by a bare except."""
        pkg = modules_root / "demo" / "sample"
        pkg.mkdir(parents=True)
        (pkg / "manifest.json").write_text("{ not json at all", encoding="utf-8")
        registry = _Registry()

        await registry.discover_and_load()

        assert registry.degraded_modules(), "a broken manifest vanished without trace"


# ─── Concurrent discovery (RFC 0004) ──────────────────────────────────────────


class TestConcurrentDiscovery:
    """``discover_and_load`` is awaited per browser client, so it races itself.

    ``_load_from_manifest`` awaits ``on_load()`` before recording the module, so
    without a lock two clients connecting together both pass the "already
    loaded?" check, both construct the module and both let its ``on_load()``
    subscribe the same EventBus handlers — making one user action run the whole
    operation twice.
    """

    async def test_two_concurrent_callers_load_each_module_once(
        self, modules_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_manifest(modules_root)
        created: list[_StubModule] = []

        class _SlowModule(_StubModule):
            async def on_load(self) -> None:
                # Any await here is enough to interleave the two callers; the
                # real on_load() awaits logic.register().
                await asyncio.sleep(0)

        class _Package:
            @staticmethod
            def create() -> _StubModule:
                module = _SlowModule()
                created.append(module)
                return module

        monkeypatch.setattr(registry_mod.importlib, "import_module", lambda _path: _Package)
        reg = _Registry()

        await asyncio.gather(reg.discover_and_load(), reg.discover_and_load())

        assert len(created) == 1
        assert len(reg.all_modules()) == 1

    async def test_a_second_call_after_the_first_still_loads_nothing_twice(
        self, modules_root: Path, stub_import: list[_StubModule]
    ) -> None:
        _write_manifest(modules_root)
        reg = _Registry()

        await reg.discover_and_load()
        await reg.discover_and_load()

        assert len(stub_import) == 1
