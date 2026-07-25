"""Plugin registry — discovers, imports, validates, and manages modules.

Walk order: ``modules/**/manifest.json``
Load sequence per module:
  1. Validate manifest schema + Python version.
  2. ``importlib.import_module()`` the module package.
  3. Call the module's ``create()`` factory.
  4. Call ``on_load()`` — if it raises, mark the module DEGRADED.
  5. Publish ``ModuleLoadedEvent`` or ``ModuleDegradedEvent`` on EventBus.

Note: ``pip_dependencies`` in the manifest is metadata only — it is *not*
verified here. Each module's ``on_load()`` checks the imports it actually needs
and degrades with install guidance (rule B-06), which reports the real problem
rather than guessing at a distribution-name-to-import-name mapping. A registry
level check remains a documented Phase-0 deferral.

Degraded modules are visible in the UI with a warning icon but never
crash the host application (rule B-06).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Final

from core.base_module import BaseModule
from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ModuleDegradedEvent, ModuleLoadedEvent, ModuleMetadata
from shared.constants import APP_ROOT, SHIPPED_TIERS, VALID_TIERS

logger = get_logger(__name__)

#: Where module packages are discovered. Derived from ``APP_ROOT`` rather than
#: from ``__file__`` so it survives a PyInstaller freeze: under a freeze
#: ``__file__`` points inside the read-only ``_MEI`` bundle whose layout does
#: not match the source tree, and discovery would silently find nothing —
#: an application with no modules at all. ``APP_ROOT`` already resolves the
#: frozen case (see :func:`shared.constants._resolve_app_root`).
_MODULES_ROOT: Final[Path] = APP_ROOT / "modules"
_MANIFEST_FILENAME: Final[str] = "manifest.json"
_REQUIRED_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "id", "version", "description", "author", "pillar", "icon",
     "tags", "min_python_version", "requires_elevation", "external_binaries",
     "pip_dependencies", "disabled_on", "experimental", "tier"}
)


class _Registry:
    """Singleton registry of all loaded BaseModule instances."""

    def __init__(self) -> None:
        self._modules: dict[str, BaseModule] = {}
        self._degraded: dict[str, str] = {}  # module_id → reason
        self._metadata: dict[str, ModuleMetadata] = {}
        self._load_lock: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        """Return the discovery lock, created on the running event loop.

        Built lazily rather than in ``__init__`` because this is a module-level
        singleton instantiated at import time, when there is no running loop to
        bind a lock to.

        Returns:
            The shared discovery lock.
        """
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        return self._load_lock

    async def discover_and_load(self) -> None:
        """Walk ``modules/`` and load every valid manifest.

        Safe to call multiple times — already-loaded modules are skipped — and
        safe to call concurrently. The page handler runs this per browser
        client, so two clients connecting at once both used to pass the
        "already loaded?" check before either finished loading: every module
        was constructed twice and every ``on_load()`` subscribed its EventBus
        handlers a second time, which made a single user action run the whole
        operation twice. The lock makes check-and-load one indivisible step;
        the second caller then finds the modules present and does nothing.
        """
        if not _MODULES_ROOT.exists():
            logger.warning("registry.modules_root_missing — path=%s", _MODULES_ROOT)
            return

        async with self._lock():
            for manifest_path in sorted(_MODULES_ROOT.rglob(_MANIFEST_FILENAME)):
                module_id = self._peek_module_id(manifest_path)
                if module_id in self._modules or module_id in self._degraded:
                    continue
                await self._load_from_manifest(manifest_path)

    def _peek_module_id(self, manifest_path: Path) -> str:
        """Read only the ``id`` field from a manifest without full parsing.

        Args:
            manifest_path: Path to the manifest.json file.

        Returns:
            The module ID string, or an empty string on read failure.
        """
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # An empty id makes the caller fall through to the full load, which
            # reports the real failure and marks the module DEGRADED. Silence
            # here previously hid a malformed manifest entirely.
            logger.debug(
                "registry.manifest_peek_failed — path=%s reason=%s", manifest_path, exc
            )
            return ""
        return str(data.get("id", "")) if isinstance(data, dict) else ""

    async def _load_from_manifest(self, manifest_path: Path) -> None:
        """Attempt to load a single module from its manifest.

        Args:
            manifest_path: Absolute path to ``manifest.json``.
        """
        module_id = "<unknown>"
        try:
            raw = manifest_path.read_text(encoding="utf-8")
            manifest: dict[str, object] = json.loads(raw)
            module_id = str(manifest.get("id", "unknown"))

            # Schema validation
            missing = _REQUIRED_MANIFEST_KEYS - manifest.keys()
            if missing:
                raise ValueError(f"manifest missing keys: {missing}")

            # Platform exclusion
            raw_disabled = manifest.get("disabled_on", [])
            disabled_on = (
                [str(p) for p in raw_disabled] if isinstance(raw_disabled, list) else []
            )
            if sys.platform in disabled_on:
                logger.info("registry.skipped_platform — module=%s", module_id)
                return

            # Version tier gate (RFC 0007). OmniForge ships as tiers; only the
            # shipped tier's modules load. An unknown tier is a manifest error;
            # a valid-but-unshipped tier is skipped BEFORE importing the package,
            # because a parked module may import dependencies this build no
            # longer installs — importing it to read its tier would raise.
            tier = str(manifest.get("tier", ""))
            if tier not in VALID_TIERS:
                raise ValueError(
                    f"tier {tier!r} is not one of {sorted(VALID_TIERS)}"
                )
            if tier not in SHIPPED_TIERS:
                logger.info(
                    "registry.skipped_tier — module=%s tier=%s", module_id, tier
                )
                return

            # Python version check. Only major.minor is compared: a manifest
            # saying "3.11.0" would otherwise build a 3-tuple that can never
            # be >= sys.version_info[:2], failing every module that declared a
            # patch level. A non-numeric value degrades with a clear reason
            # instead of raising ValueError from int().
            min_ver = str(manifest.get("min_python_version", "3.11"))
            try:
                required = tuple(int(part) for part in min_ver.split(".")[:2])
            except ValueError as exc:
                raise RuntimeError(
                    f"min_python_version {min_ver!r} is not a version number"
                ) from exc
            if sys.version_info[: len(required)] < required:
                raise RuntimeError(
                    f"requires Python {min_ver}, running {sys.version_info[:2]}"
                )

            # Derive import path: modules/<pillar>/<name>
            rel = manifest_path.parent.relative_to(_MODULES_ROOT.parent)
            import_path = ".".join(rel.parts)

            mod = importlib.import_module(import_path)
            factory = getattr(mod, "create", None)
            if factory is None or not callable(factory):
                raise AttributeError(f"module package {import_path!r} missing create()")

            instance: BaseModule = factory()
            await instance.on_load()

            self._modules[module_id] = instance
            self._metadata[module_id] = _metadata_from_manifest(module_id, manifest)
            await event_bus.publish(
                "registry.module_loaded",
                ModuleLoadedEvent(
                    module_id=module_id,
                    name=str(manifest["name"]),
                    pillar=str(manifest["pillar"]),
                ),
            )
            logger.info("registry.loaded — module=%s", module_id)

        except Exception as exc:
            reason = str(exc)
            self._degraded[module_id] = reason
            logger.error(
                "registry.degraded — module=%s reason=%s",
                module_id,
                reason,
                exc_info=exc,
            )
            await event_bus.publish(
                "registry.module_degraded",
                ModuleDegradedEvent(module_id=module_id, reason=reason),
            )

    def get(self, module_id: str) -> BaseModule | None:
        """Return the loaded module for *module_id*, or ``None``.

        Args:
            module_id: Dot-separated module identifier.

        Returns:
            BaseModule instance or None if not loaded/degraded.
        """
        return self._modules.get(module_id)

    def all_metadata(self) -> list[ModuleMetadata]:
        """Return manifest metadata for every loaded module.

        Returns:
            List of ModuleMetadata, ordered by discovery. Used by the command
            palette and sidebar search to index names, descriptions and tags.
        """
        return list(self._metadata.values())

    def all_modules(self) -> list[BaseModule]:
        """Return all successfully loaded module instances.

        Returns:
            List of BaseModule instances, ordered by discovery.
        """
        return list(self._modules.values())

    def degraded_modules(self) -> dict[str, str]:
        """Return degraded module IDs mapped to their failure reasons.

        Returns:
            Dict of module_id → reason string.
        """
        return dict(self._degraded)

    async def unload_all(self) -> None:
        """Call ``on_unload()`` on every loaded module.

        Called on application shutdown. Errors are logged but not re-raised.
        """
        for module_id, module in self._modules.items():
            try:
                await module.on_unload()
            except Exception as exc:
                logger.error("registry.unload_error — module=%s", module_id, exc_info=exc)
        self._modules.clear()
        self._metadata.clear()


def _metadata_from_manifest(module_id: str, manifest: dict[str, object]) -> ModuleMetadata:
    """Build a ModuleMetadata from a validated manifest dict.

    Args:
        module_id: The module's identifier.
        manifest: The parsed manifest.

    Returns:
        Metadata with tags coerced to a list of strings.
    """
    raw_tags = manifest.get("tags", [])
    tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
    return ModuleMetadata(
        module_id=module_id,
        name=str(manifest.get("name", module_id)),
        pillar=str(manifest.get("pillar", "")),
        description=str(manifest.get("description", "")),
        tags=tags,
        experimental=bool(manifest.get("experimental", False)),
    )


#: Singleton instance — import and use this everywhere.
registry = _Registry()
