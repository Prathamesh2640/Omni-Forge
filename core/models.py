"""Shared Pydantic v2 models for OmniForge inter-component communication.

These models flow over the EventBus and through the Registry.
Module-specific input/output models belong in each module's ``models.py``.

Note there is no generic execute/cancel request model. Each module publishes
its own typed params to its own topic (``<pillar>.<module>.execute``), which
is what gives the handler a real type to validate against — a shared
``dict``-carrying envelope would have thrown that away.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class ProgressEvent(BaseModel):
    """Emitted by a module's ``execute()`` at each progress checkpoint.

    Every long-running operation MUST yield at least 10 of these
    (rule D-08) so the UI always shows meaningful feedback.

    Attributes:
        percent: Completion percentage, 0-100 (inclusive).
        message: Human-readable status description shown in the UI.
        output_path: Path to the completed artifact when ``percent == 100``.
        error: User-readable failure description; set only on failure.
        cancelled: ``True`` when the operation was cancelled by the user.
    """

    percent: int = Field(ge=0, le=100)
    message: str
    output_path: Path | None = None
    error: str | None = None
    cancelled: bool = False


class ModuleMetadata(BaseModel):
    """Manifest facts the Registry retains after a module is loaded.

    ``BaseModule`` exposes only identity; the searchable fields (description,
    tags) live in the manifest, so the Registry keeps them here for the
    command palette and module search to index.

    Attributes:
        module_id: Dot-separated module identifier.
        name: Human-readable module name.
        pillar: The pillar this module belongs to.
        description: One-sentence summary from the manifest.
        tags: Keywords used for search.
        experimental: True when the manifest marks the module experimental.
    """

    module_id: str
    name: str
    pillar: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    experimental: bool = False


class ModuleLoadedEvent(BaseModel):
    """Published by the Registry after a module is successfully loaded.

    Attributes:
        module_id: Dot-separated module identifier.
        name: Human-readable module name from the manifest.
        pillar: The pillar this module belongs to.
    """

    module_id: str
    name: str
    pillar: str


class ModuleDegradedEvent(BaseModel):
    """Published by the Registry when a module fails to load.

    Attributes:
        module_id: Dot-separated module identifier (from the manifest).
        reason: Human-readable explanation of why the module is degraded.
    """

    module_id: str
    reason: str
