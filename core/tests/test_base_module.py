"""Unit tests for core.base_module's concrete behaviour.

The abstract contract is exercised by every real module; what is tested here
is the per-client UI lifecycle BaseModule provides on their behalf.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.base_module import BaseModule
from core.models import ProgressEvent

# ─── §3.9 — per-client UI controllers ────────────────────────────────────────


class _SpyUI:
    """Records its own subscribe/unsubscribe calls."""

    def __init__(self) -> None:
        self.subscribed = False
        self.unsubscribed = False

    def subscribe(self) -> None:
        self.subscribed = True

    def unsubscribe(self) -> None:
        self.unsubscribed = True


class _HostModule(BaseModule):
    """Minimal module used to exercise the UI-attachment lifecycle."""

    @property
    def module_id(self) -> str:
        return "demo.host"

    @property
    def name(self) -> str:
        return "Host"

    @property
    def pillar(self) -> str:
        return "demo"

    @property
    def icon(self) -> str:
        return "assets/icons/demo.svg"

    async def on_load(self) -> None:
        return None

    async def on_unload(self) -> None:
        return None

    async def execute(self, params: Any) -> AsyncIterator[ProgressEvent]:
        yield ProgressEvent(percent=100, message="done")

    def build_ui(self, container: Any) -> None:
        return None


class TestPerClientUi:
    """One module instance serves every client, so its UI cannot be shared.

    A single controller meant the last client to render owned the element
    references, and each re-render leaked another set of subscriptions.
    """

    def test_attach_subscribes_the_new_controller(self) -> None:
        module = _HostModule()
        created = module.attach_ui(_SpyUI)
        assert created.subscribed is True

    def test_rerender_releases_the_previous_controller(self) -> None:
        """Navigating away and back must not accumulate handlers."""
        module = _HostModule()
        first = module.attach_ui(_SpyUI)

        second = module.attach_ui(_SpyUI)

        assert first.unsubscribed is True
        assert second.unsubscribed is False
        assert second is not first

    def test_detach_releases_everything(self) -> None:
        module = _HostModule()
        controller = module.attach_ui(_SpyUI)

        module.detach_ui()

        assert controller.unsubscribed is True

    def test_detach_is_idempotent(self) -> None:
        module = _HostModule()
        module.attach_ui(_SpyUI)
        module.detach_ui()
        module.detach_ui()  # must not raise

    def test_detach_without_any_ui_is_safe(self) -> None:
        _HostModule().detach_ui()

    def test_a_failing_teardown_does_not_propagate(self) -> None:
        """Teardown runs on disconnect; it must never break that path."""

        class _BadUI(_SpyUI):
            def unsubscribe(self) -> None:
                raise RuntimeError("teardown exploded")

        module = _HostModule()
        module.attach_ui(_BadUI)
        module.detach_ui()  # must not raise

    def test_separate_modules_keep_separate_registries(self) -> None:
        first, second = _HostModule(), _HostModule()
        a = first.attach_ui(_SpyUI)
        second.attach_ui(_SpyUI)

        second.detach_ui()

        assert a.unsubscribed is False
