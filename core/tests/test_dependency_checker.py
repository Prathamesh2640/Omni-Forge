"""Unit tests for core.dependency_checker (WebView2 + pywebview validation)."""
from __future__ import annotations

import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest

from core import dependency_checker
from core.dependency_checker import (
    _is_pywebview_installed,
    _is_webview2_installed,
    validate_startup,
)

# Registry view flags, mirroring winreg's real values.
_KEY_READ = 0x20019
_WOW64_32 = 0x0200
_WOW64_64 = 0x0100


class _FakeKey:
    """Handle returned by a successful OpenKey."""

    def Close(self) -> None:
        """Release the handle."""


def _fake_winreg(found_at: set[tuple[int, int]] | None = None) -> ModuleType:
    """Build a stand-in winreg module.

    Args:
        found_at: ``(hive, view)`` pairs where the key should be found.
            Empty or None means the key exists nowhere.
    """
    module = ModuleType("winreg")
    module.HKEY_LOCAL_MACHINE = 0  # type: ignore[attr-defined]
    module.HKEY_CURRENT_USER = 1  # type: ignore[attr-defined]
    module.KEY_READ = _KEY_READ  # type: ignore[attr-defined]
    module.KEY_WOW64_32KEY = _WOW64_32  # type: ignore[attr-defined]
    module.KEY_WOW64_64KEY = _WOW64_64  # type: ignore[attr-defined]
    module.probed = []  # type: ignore[attr-defined]

    located = found_at or set()

    def open_key(hive: int, _path: str, _reserved: int = 0, access: int = _KEY_READ) -> _FakeKey:
        view = access & (_WOW64_32 | _WOW64_64)
        module.probed.append((hive, view))  # type: ignore[attr-defined]
        if (hive, view) in located:
            return _FakeKey()
        raise OSError("registry key not found")

    module.OpenKey = open_key  # type: ignore[attr-defined]
    return module


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the platform check down the Windows branch."""
    monkeypatch.setattr(dependency_checker.sys, "platform", "win32")


@pytest.fixture
def install_fake_winreg(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Allow a test to inject a fake winreg into the import system."""

    def install(found_at: set[tuple[int, int]] | None = None) -> ModuleType:
        module = _fake_winreg(found_at)
        monkeypatch.setitem(sys.modules, "winreg", module)
        return module

    yield install


# ─── Platform gating ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_non_windows_platforms_never_require_webview2(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(dependency_checker.sys, "platform", platform)
    assert _is_webview2_installed() is True


# ─── Registry views (the 32-bit redirection bug) ──────────────────────────────


def test_detects_a_machine_wide_install_in_the_32_bit_view(
    on_windows: None, install_fake_winreg: Any
) -> None:
    """EdgeUpdate is 32-bit, so HKLM registrations land under WOW6432Node.

    A 64-bit Python is redirected away from that key unless KEY_WOW64_32KEY
    is requested, which previously reported an installed runtime as missing.
    """
    install_fake_winreg({(0, _WOW64_32)})
    assert _is_webview2_installed() is True


def test_detects_an_install_in_the_64_bit_view(
    on_windows: None, install_fake_winreg: Any
) -> None:
    install_fake_winreg({(0, _WOW64_64)})
    assert _is_webview2_installed() is True


def test_detects_a_per_user_install(on_windows: None, install_fake_winreg: Any) -> None:
    install_fake_winreg({(1, _WOW64_32)})
    assert _is_webview2_installed() is True


def test_probes_both_hives_in_both_views_before_giving_up(
    on_windows: None, install_fake_winreg: Any
) -> None:
    module = install_fake_winreg(set())

    assert _is_webview2_installed() is False
    assert set(module.probed) == {  # type: ignore[attr-defined]
        (0, _WOW64_32), (0, _WOW64_64), (1, _WOW64_32), (1, _WOW64_64),
    }


def test_reports_missing_when_no_view_has_the_key(
    on_windows: None, install_fake_winreg: Any
) -> None:
    install_fake_winreg(set())
    assert _is_webview2_installed() is False


def test_unavailable_winreg_is_reported_as_missing(
    on_windows: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build without winreg must fail the check, not crash startup."""
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert _is_webview2_installed() is False


# ─── pywebview ────────────────────────────────────────────────────────────────


def test_detects_an_installed_pywebview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dependency_checker.importlib.util, "find_spec", lambda _name: object()
    )
    assert _is_pywebview_installed() is True


def test_detects_a_missing_pywebview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dependency_checker.importlib.util, "find_spec", lambda _name: None
    )
    assert _is_pywebview_installed() is False


# ─── validate_startup ─────────────────────────────────────────────────────────


@pytest.fixture
def satisfied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report every native-mode dependency as present."""
    monkeypatch.setattr(dependency_checker, "_is_webview2_installed", lambda: True)
    monkeypatch.setattr(dependency_checker, "_is_pywebview_installed", lambda: True)


def test_no_errors_when_dependencies_are_satisfied(satisfied: None) -> None:
    assert validate_startup() == []


def test_reports_missing_webview2(
    satisfied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dependency_checker, "_is_webview2_installed", lambda: False)
    errors = validate_startup()

    assert len(errors) == 1
    assert "WebView2 Runtime is required" in errors[0]


def test_reports_missing_pywebview(
    satisfied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dependency_checker, "_is_pywebview_installed", lambda: False)
    errors = validate_startup()

    assert len(errors) == 1
    assert "pywebview" in errors[0]


def test_reports_both_failures_independently(
    satisfied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dependency_checker, "_is_webview2_installed", lambda: False)
    monkeypatch.setattr(dependency_checker, "_is_pywebview_installed", lambda: False)
    assert len(validate_startup()) == 2


def test_webview2_message_includes_the_download_link(
    satisfied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dependency_checker, "_is_webview2_installed", lambda: False)
    assert "developer.microsoft.com" in validate_startup()[0]


@pytest.mark.parametrize(
    "failing", ["_is_webview2_installed", "_is_pywebview_installed"]
)
def test_every_failure_offers_the_browser_fallback(
    satisfied: None, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """A blocked native launch must not look like a dead end."""
    monkeypatch.setattr(dependency_checker, failing, lambda: False)
    assert "--browser" in validate_startup()[0]
