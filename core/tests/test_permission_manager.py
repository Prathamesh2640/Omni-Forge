"""Unit tests for core.permission_manager (rule B-05).

Every platform-specific call is mocked — these tests never actually elevate.
"""
from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core import permission_manager
from core.permission_manager import (
    ElevationOutcome,
    ElevationRequest,
    ElevationResult,
    _decode,
    _result_from_exit_code,
    describe,
    elevate,
    find_escalation_binary,
    is_elevated,
    resolve_executable,
)
from shared.constants import (
    ELEVATION_STDERR_MAX_CHARS,
    ERROR_CANCELLED,
    WAIT_FAILED,
    WAIT_TIMEOUT,
)


class _FakeShell32:
    """Records ShellExecuteExW calls and returns a scripted result."""

    def __init__(self, launch_ok: bool = True, admin_result: int = 0) -> None:
        self.launch_ok = launch_ok
        self.admin_result = admin_result
        self.launched: list[Any] = []
        self.ShellExecuteExW = _FakeFuncPtr(self._shell_execute)
        self.IsUserAnAdmin = _FakeFuncPtr(lambda: self.admin_result)

    def _shell_execute(self, info_ref: Any) -> int:
        self.launched.append(info_ref._obj)
        return 1 if self.launch_ok else 0


class _FakeFuncPtr:
    """Stand-in for a ctypes function pointer.

    A real entry point carries settable ``argtypes`` *and* ``restype``, and
    production code declares both — a HANDLE is pointer-sized, so leaving
    ctypes to assume a C int truncates it on 64-bit Windows. A bound method
    cannot take those attributes, so every faked function needs this wrapper.
    """

    def __init__(self, implementation: Any) -> None:
        self._implementation = implementation
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        return self._implementation(*args)


class _FakeKernel32:
    """Scriptable stand-in for the kernel32 process-wait APIs."""

    def __init__(
        self,
        wait_result: int = 0,
        exit_code: int = 0,
        read_exit_ok: bool = True,
    ) -> None:
        self.wait_result = wait_result
        self.exit_code = exit_code
        self.read_exit_ok = read_exit_ok
        self.closed_handles: list[Any] = []
        # A real ctypes entry point is a _FuncPtr object carrying a settable
        # `restype`, not a bound method — production code sets that to DWORD so
        # a failed wait compares equal to WAIT_FAILED (0xFFFFFFFF) instead of
        # arriving as a signed -1. Expose the same shape here.
        self.WaitForSingleObject = _FakeFuncPtr(lambda _handle, _ms: self.wait_result)
        self.GetExitCodeProcess = _FakeFuncPtr(self._get_exit_code)
        self.CloseHandle = _FakeFuncPtr(self._close_handle)

    def _get_exit_code(self, _handle: Any, out_ref: Any) -> int:
        if not self.read_exit_ok:
            return 0
        out_ref._obj.value = self.exit_code
        return 1

    def _close_handle(self, handle: Any) -> int:
        self.closed_handles.append(handle)
        return 1


class _FakeWinDll:
    """Minimal ctypes.windll replacement exposing shell32 and kernel32."""

    def __init__(
        self,
        launch_ok: bool = True,
        admin_result: int = 0,
        wait_result: int = 0,
        exit_code: int = 0,
        read_exit_ok: bool = True,
    ) -> None:
        self.shell32 = _FakeShell32(launch_ok=launch_ok, admin_result=admin_result)
        self.kernel32 = _FakeKernel32(
            wait_result=wait_result, exit_code=exit_code, read_exit_ok=read_exit_ok
        )


def _request(**overrides: Any) -> ElevationRequest:
    """Build a valid elevation request, applying *overrides*."""
    defaults: dict[str, Any] = {
        "executable": "sysctl",
        "arguments": ["-w", "vm.drop_caches=3"],
        "reason": "Free cached memory",
    }
    return ElevationRequest(**{**defaults, **overrides})


@pytest.fixture
def not_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report the current process as unprivileged."""
    monkeypatch.setattr(permission_manager, "is_elevated", lambda: False)


@pytest.fixture
def on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the POSIX elevation branch."""
    monkeypatch.setattr(permission_manager.sys, "platform", "linux")


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the Windows elevation branch."""
    monkeypatch.setattr(permission_manager.sys, "platform", "win32")


class _CompletedProcess:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


# ─── Request validation ───────────────────────────────────────────────────────


class TestElevationRequest:
    def test_reason_is_mandatory(self) -> None:
        """An elevation with no stated reason is a blind elevation (rule B-05)."""
        with pytest.raises(ValidationError):
            ElevationRequest(executable="rm", reason="")

    def test_executable_is_mandatory(self) -> None:
        with pytest.raises(ValidationError):
            ElevationRequest(executable="", reason="because")

    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _request(timeout_seconds=0)

    def test_arguments_default_to_empty(self) -> None:
        assert ElevationRequest(executable="whoami", reason="check").arguments == []


# ─── describe() — what the user is shown ──────────────────────────────────────


class TestDescribe:
    def test_renders_the_full_command_line(self) -> None:
        assert describe(_request()) == "sysctl -w vm.drop_caches=3"

    def test_bare_executable_has_no_trailing_space(self) -> None:
        assert describe(_request(executable="whoami", arguments=[])) == "whoami"

    def test_arguments_with_spaces_are_quoted(self) -> None:
        described = describe(
            _request(executable="rm", arguments=["-rf", "/tmp/my folder"])
        )
        assert "'/tmp/my folder'" in described

    def test_shell_metacharacters_are_quoted(self) -> None:
        """The preview must not look like something it is not."""
        described = describe(_request(executable="echo", arguments=["a; rm -rf /"]))
        assert described == "echo 'a; rm -rf /'"


# ─── is_elevated() ────────────────────────────────────────────────────────────


class TestIsElevated:
    def test_posix_root_is_elevated(
        self, on_posix: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(permission_manager.os, "geteuid", lambda: 0, raising=False)
        assert is_elevated() is True

    def test_posix_regular_user_is_not_elevated(
        self, on_posix: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(permission_manager.os, "geteuid", lambda: 1000, raising=False)
        assert is_elevated() is False

    @pytest.mark.parametrize(("api_result", "expected"), [(1, True), (0, False)])
    def test_windows_reflects_is_user_an_admin(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch,
        api_result: int, expected: bool,
    ) -> None:
        # is_elevated() loads shell32 with ctypes.WinDLL(..., use_last_error=True)
        # so it can declare a real signature, the same way _elevate_windows does.
        fake = _FakeWinDll(admin_result=api_result)
        monkeypatch.setattr(
            ctypes, "WinDLL", lambda name, *_a, **_kw: getattr(fake, name), raising=False
        )
        assert is_elevated() is expected

    def test_windows_detection_failure_assumes_unprivileged(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assuming the lesser privilege is the safe default."""

        def refuse(_name: str, *_args: Any, **_kwargs: Any) -> object:
            raise OSError("shell32 unavailable")

        monkeypatch.setattr(ctypes, "WinDLL", refuse, raising=False)
        assert is_elevated() is False


# ─── find_escalation_binary() ─────────────────────────────────────────────────


class TestFindEscalationBinary:
    def test_prefers_pkexec_over_sudo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            permission_manager.shutil, "which", lambda name: f"/usr/bin/{name}"
        )
        assert find_escalation_binary() == "/usr/bin/pkexec"

    def test_falls_back_to_sudo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            permission_manager.shutil,
            "which",
            lambda name: "/usr/bin/sudo" if name == "sudo" else None,
        )
        assert find_escalation_binary() == "/usr/bin/sudo"

    def test_returns_none_when_neither_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(permission_manager.shutil, "which", lambda _name: None)
        assert find_escalation_binary() is None


# ─── elevate() on POSIX ───────────────────────────────────────────────────────


class TestElevatePosix:
    @pytest.fixture(autouse=True)
    def _setup(
        self, not_elevated: None, on_posix: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            permission_manager, "find_escalation_binary", lambda: "/usr/bin/pkexec"
        )

    def test_success_reports_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            permission_manager.subprocess, "run", lambda *a, **k: _CompletedProcess(0)
        )
        result = elevate(_request())

        assert result.outcome is ElevationOutcome.SUCCEEDED
        assert result.ok is True

    def test_the_escalation_helper_wraps_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: Any) -> _CompletedProcess:
            captured.append(argv)
            return _CompletedProcess(0)

        monkeypatch.setattr(permission_manager.subprocess, "run", fake_run)
        elevate(_request())

        assert captured[0] == ["/usr/bin/pkexec", "sysctl", "-w", "vm.drop_caches=3"]

    def test_nonzero_exit_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            permission_manager.subprocess, "run", lambda *a, **k: _CompletedProcess(2)
        )
        result = elevate(_request())

        assert result.outcome is ElevationOutcome.FAILED
        assert result.exit_code == 2
        assert result.ok is False

    @pytest.mark.parametrize("code", [126, 127])
    def test_auth_refusal_is_reported_as_cancelled(
        self, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        """pkexec and sudo both use 126/127 for a declined authorisation."""
        monkeypatch.setattr(
            permission_manager.subprocess, "run", lambda *a, **k: _CompletedProcess(code)
        )
        assert elevate(_request()).outcome is ElevationOutcome.CANCELLED

    def test_timeout_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def timeout(*_args: Any, **_kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd="pkexec", timeout=120)

        monkeypatch.setattr(permission_manager.subprocess, "run", timeout)
        result = elevate(_request())

        assert result.outcome is ElevationOutcome.TIMED_OUT

    def test_os_error_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("exec format error")

        monkeypatch.setattr(permission_manager.subprocess, "run", explode)
        assert elevate(_request()).outcome is ElevationOutcome.FAILED

    def test_missing_helper_reports_unavailable_with_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(permission_manager, "find_escalation_binary", lambda: None)
        result = elevate(_request())

        assert result.outcome is ElevationOutcome.UNAVAILABLE
        assert "pkexec" in result.message


# ─── elevate() on Windows (ShellExecuteEx / UAC) ──────────────────────────────


class TestElevateWindows:
    """Exercises the ctypes plumbing with a faked ``ctypes.windll``."""

    @pytest.fixture
    def install_windll(
        self, not_elevated: None, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> Any:
        def install(**kwargs: Any) -> _FakeWinDll:
            fake = _FakeWinDll(**kwargs)

            # _elevate_windows now loads each library via
            # ctypes.WinDLL(name, use_last_error=True); return the matching fake.
            def load(name: str, *_args: Any, **_kwargs: Any) -> Any:
                return getattr(fake, name)

            monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
            return fake

        return install

    def test_successful_run_reports_ok(self, install_windll: Any) -> None:
        install_windll(exit_code=0)
        assert elevate(_request()).ok is True

    def test_nonzero_exit_reports_failure(self, install_windll: Any) -> None:
        install_windll(exit_code=3)
        result = elevate(_request())

        assert result.outcome is ElevationOutcome.FAILED
        assert result.exit_code == 3

    def test_dismissed_uac_prompt_is_reported_as_cancelled(
        self, install_windll: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_windll(launch_ok=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: ERROR_CANCELLED)

        result = elevate(_request())

        assert result.outcome is ElevationOutcome.CANCELLED
        assert "dismissed" in result.message

    def test_other_launch_error_is_reported_as_failure(
        self, install_windll: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_windll(launch_ok=False)
        monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)

        result = elevate(_request())

        assert result.outcome is ElevationOutcome.FAILED
        assert "error 5" in result.message

    def test_wait_timeout_is_reported(self, install_windll: Any) -> None:
        install_windll(wait_result=WAIT_TIMEOUT)
        assert elevate(_request()).outcome is ElevationOutcome.TIMED_OUT

    def test_wait_failure_is_reported(self, install_windll: Any) -> None:
        install_windll(wait_result=WAIT_FAILED)
        assert elevate(_request()).outcome is ElevationOutcome.FAILED

    def test_unreadable_exit_code_is_reported(self, install_windll: Any) -> None:
        install_windll(read_exit_ok=False)
        result = elevate(_request())

        assert result.outcome is ElevationOutcome.FAILED
        assert "exit code" in result.message

    def test_the_runas_verb_triggers_uac(self, install_windll: Any) -> None:
        fake = install_windll()
        elevate(_request())
        assert fake.shell32.launched[0].lpVerb == "runas"

    def test_the_requested_command_is_what_gets_elevated(
        self, install_windll: Any
    ) -> None:
        fake = install_windll()
        elevate(_request(executable="sysctl", arguments=["-w", "vm.drop_caches=3"]))

        info = fake.shell32.launched[0]
        assert info.lpFile == "sysctl"
        assert info.lpParameters == "-w vm.drop_caches=3"

    def test_the_process_handle_is_always_closed(self, install_windll: Any) -> None:
        """Leaking the handle would leak a kernel object on every elevation."""
        fake = install_windll(exit_code=0)
        elevate(_request())
        assert len(fake.kernel32.closed_handles) == 1

    def test_handle_is_closed_even_on_timeout(self, install_windll: Any) -> None:
        fake = install_windll(wait_result=WAIT_TIMEOUT)
        elevate(_request())
        assert len(fake.kernel32.closed_handles) == 1


# ─── elevate() when already privileged ────────────────────────────────────────


class TestAlreadyElevated:
    def test_runs_directly_without_an_escalation_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(permission_manager, "is_elevated", lambda: True)
        captured: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: Any) -> _CompletedProcess:
            captured.append(argv)
            return _CompletedProcess(0)

        monkeypatch.setattr(permission_manager.subprocess, "run", fake_run)
        result = elevate(_request())

        assert captured[0] == ["sysctl", "-w", "vm.drop_caches=3"]
        assert result.ok is True

    def test_direct_run_timeout_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(permission_manager, "is_elevated", lambda: True)

        def timeout(*_args: Any, **_kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd="sysctl", timeout=120)

        monkeypatch.setattr(permission_manager.subprocess, "run", timeout)
        assert elevate(_request()).outcome is ElevationOutcome.TIMED_OUT

    def test_direct_run_os_error_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(permission_manager, "is_elevated", lambda: True)

        def explode(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("not found")

        monkeypatch.setattr(permission_manager.subprocess, "run", explode)
        assert elevate(_request()).outcome is ElevationOutcome.FAILED


# ─── ElevationResult ──────────────────────────────────────────────────────────


class TestElevationResult:
    def test_ok_requires_both_success_and_zero_exit(self) -> None:
        assert ElevationResult(
            outcome=ElevationOutcome.SUCCEEDED, exit_code=0, message=""
        ).ok is True

    @pytest.mark.parametrize(
        ("outcome", "exit_code"),
        [
            (ElevationOutcome.SUCCEEDED, 1),
            (ElevationOutcome.FAILED, 0),
            (ElevationOutcome.CANCELLED, None),
            (ElevationOutcome.TIMED_OUT, None),
            (ElevationOutcome.UNAVAILABLE, None),
        ],
    )
    def test_every_other_combination_is_not_ok(
        self, outcome: ElevationOutcome, exit_code: int | None
    ) -> None:
        assert ElevationResult(outcome=outcome, exit_code=exit_code, message="").ok is False


# ─── resolve_executable() ─────────────────────────────────────────────────────


class TestResolveExecutable:
    def test_returns_a_path_when_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            permission_manager.shutil, "which", lambda _n: "/usr/bin/ffmpeg"
        )
        assert resolve_executable("ffmpeg") == Path("/usr/bin/ffmpeg")

    def test_returns_none_when_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(permission_manager.shutil, "which", lambda _n: None)
        assert resolve_executable("nonexistent-binary") is None


# ─── §3.3 / §3.12f — reachable WAIT_FAILED, and stderr the user can act on ────


class TestFailureDiagnostics:
    def test_stderr_is_surfaced_in_the_failure_message(self) -> None:
        """"exited with code 1" alone gives the user nothing to act on."""
        result = _result_from_exit_code(1, "sync: cannot open /proc/sys/vm: denied")

        assert result.outcome is ElevationOutcome.FAILED
        assert "cannot open /proc/sys/vm" in result.message

    def test_a_successful_run_does_not_quote_stderr(self) -> None:
        """Plenty of tools write progress to stderr while succeeding."""
        result = _result_from_exit_code(0, "warning: deprecated flag")

        assert result.outcome is ElevationOutcome.SUCCEEDED
        assert "deprecated" not in result.message

    def test_long_stderr_is_truncated(self) -> None:
        result = _result_from_exit_code(1, "x" * (ELEVATION_STDERR_MAX_CHARS + 500))

        assert len(result.message) < ELEVATION_STDERR_MAX_CHARS + 100
        assert result.message.endswith("…")

    def test_stderr_whitespace_is_collapsed_to_one_line(self) -> None:
        result = _result_from_exit_code(1, "first line\n\n   second line\t\tthird")
        assert "first line second line third" in result.message

    def test_empty_stderr_leaves_the_message_clean(self) -> None:
        result = _result_from_exit_code(2, "   \n  ")
        assert result.message == "Command exited with code 2."

    def test_bytes_stderr_is_decoded(self) -> None:
        """subprocess returns bytes unless text=True was requested."""
        assert _decode(b"permission denied") == "permission denied"

    def test_undecodable_bytes_do_not_raise(self) -> None:
        assert _decode(b"\xff\xfe bad") != ""

    def test_missing_stderr_decodes_to_empty(self) -> None:
        assert _decode(None) == ""
