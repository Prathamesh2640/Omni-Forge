"""Unit tests for shared.platform_info."""
from __future__ import annotations

import pytest

from shared import platform_info
from shared.platform_info import (
    HostInfo,
    OperatingSystem,
    bytes_to_gib,
    current_os,
    describe_host,
    is_linux,
    is_macos,
    is_windows,
    memory_terminology,
)


@pytest.fixture
def as_platform(monkeypatch: pytest.MonkeyPatch) -> object:
    """Pretend the process is running on a given sys.platform value."""

    def apply(value: str) -> None:
        monkeypatch.setattr(platform_info.sys, "platform", value)

    return apply


class TestCurrentOs:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("win32", OperatingSystem.WINDOWS),
            ("linux", OperatingSystem.LINUX),
            ("linux2", OperatingSystem.LINUX),
            ("darwin", OperatingSystem.MACOS),
            ("freebsd13", OperatingSystem.UNKNOWN),
            ("emscripten", OperatingSystem.UNKNOWN),
        ],
    )
    def test_maps_sys_platform(
        self, as_platform: object, value: str, expected: OperatingSystem
    ) -> None:
        as_platform(value)  # type: ignore[operator]
        assert current_os() is expected


class TestPredicates:
    @pytest.mark.parametrize(
        ("value", "windows", "linux", "macos"),
        [
            ("win32", True, False, False),
            ("linux", False, True, False),
            ("darwin", False, False, True),
            ("freebsd13", False, False, False),
        ],
    )
    def test_exactly_one_predicate_matches(
        self, as_platform: object, value: str,
        windows: bool, linux: bool, macos: bool,
    ) -> None:
        as_platform(value)  # type: ignore[operator]
        assert (is_windows(), is_linux(), is_macos()) == (windows, linux, macos)


class TestDescribeHost:
    def test_returns_a_populated_record(self) -> None:
        host = describe_host()
        assert isinstance(host, HostInfo)
        assert host.display_name
        assert host.python_version

    def test_os_matches_current_os(self) -> None:
        assert describe_host().os is current_os()

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("win32", "Windows"), ("linux", "Linux"), ("darwin", "macOS")],
    )
    def test_display_name_is_human_readable(
        self, as_platform: object, value: str, expected: str
    ) -> None:
        as_platform(value)  # type: ignore[operator]
        assert describe_host().display_name == expected

    def test_unknown_platform_still_describes_itself(self, as_platform: object) -> None:
        as_platform("freebsd13")  # type: ignore[operator]
        assert describe_host().display_name == "Unknown OS"


class TestWindowsReleaseDetection:
    """platform.release() answers "10" on Windows 11 — the build disambiguates."""

    @pytest.fixture(autouse=True)
    def _on_windows(self, as_platform: object) -> None:
        as_platform("win32")  # type: ignore[operator]

    def _with_version(
        self, monkeypatch: pytest.MonkeyPatch, release: str, version: str
    ) -> None:
        monkeypatch.setattr(platform_info.platform, "release", lambda: release)
        monkeypatch.setattr(platform_info.platform, "version", lambda: version)

    def test_windows_11_is_reported_as_11(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._with_version(monkeypatch, "10", "10.0.26200")
        assert describe_host().release == "11"

    def test_the_first_windows_11_build_is_recognised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._with_version(monkeypatch, "10", "10.0.22000")
        assert describe_host().release == "11"

    def test_windows_10_stays_10(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._with_version(monkeypatch, "10", "10.0.19045")
        assert describe_host().release == "10"

    def test_an_unparsable_version_falls_back_to_the_reported_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._with_version(monkeypatch, "10", "not-a-version")
        assert describe_host().release == "10"

    def test_non_windows_release_is_left_alone(
        self, as_platform: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        as_platform("linux")  # type: ignore[operator]
        monkeypatch.setattr(platform_info.platform, "release", lambda: "6.5.0-generic")
        assert describe_host().release == "6.5.0-generic"


class TestShortLabel:
    def _host(self, **overrides: object) -> HostInfo:
        defaults: dict[str, object] = {
            "os": OperatingSystem.WINDOWS,
            "display_name": "Windows",
            "release": "11",
            "version": "10.0.26200",
            "architecture": "AMD64",
            "python_version": "3.11.9",
            "hostname": "workstation",
        }
        return HostInfo(**{**defaults, **overrides})  # type: ignore[arg-type]

    def test_combines_name_release_and_architecture(self) -> None:
        assert self._host().short_label == "Windows 11 · AMD64"

    def test_omits_a_missing_release_without_a_double_space(self) -> None:
        assert self._host(release="").short_label == "Windows · AMD64"


class TestMemoryTerminology:
    @pytest.mark.parametrize("value", ["win32", "linux", "darwin"])
    def test_every_platform_names_all_categories(
        self, as_platform: object, value: str
    ) -> None:
        as_platform(value)  # type: ignore[operator]
        terms = memory_terminology()
        assert set(terms) == {"available", "cached", "swap"}
        assert all(terms.values())

    def test_windows_calls_swap_the_page_file(self, as_platform: object) -> None:
        as_platform("win32")  # type: ignore[operator]
        assert memory_terminology()["swap"] == "Page File"

    def test_linux_uses_buff_cache(self, as_platform: object) -> None:
        as_platform("linux")  # type: ignore[operator]
        assert memory_terminology()["cached"] == "Buff/Cache"

    def test_macos_reports_wired_memory(self, as_platform: object) -> None:
        as_platform("darwin")  # type: ignore[operator]
        assert "Wired" in memory_terminology()["cached"]


class TestBytesToGib:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, 0.0), (1024**3, 1.0), (1024**3 * 16, 16.0), (1024**2, 0.0009765625)],
    )
    def test_converts_binary_gigabytes(self, value: int, expected: float) -> None:
        assert bytes_to_gib(value) == pytest.approx(expected)
