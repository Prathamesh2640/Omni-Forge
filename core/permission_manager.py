"""Privilege elevation — the only sanctioned path to run a command as root/admin.

Rule B-05: modules must never shell out to ``sudo`` themselves. They build an
:class:`ElevationRequest`, show the user :func:`describe` (the exact command
line that will be elevated), and only then call :func:`elevate`.

Platform behaviour:

- **Windows** — ``ShellExecuteEx`` with the ``runas`` verb triggers the UAC
  prompt. The process handle is kept open so the real exit code can be read
  back; dismissing UAC surfaces as :attr:`ElevationOutcome.CANCELLED`.
- **Linux / macOS** — ``pkexec`` (polkit GUI prompt) when present, otherwise
  ``sudo``.

Nothing here prompts the user on its own. Presenting :func:`describe` and
obtaining consent is the caller's responsibility, so elevation is never blind.
"""
from __future__ import annotations

import enum
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from core.logger import get_logger
from shared.constants import (
    ELEVATION_STDERR_MAX_CHARS,
    ELEVATION_TIMEOUT_SECONDS,
    ERROR_CANCELLED,
    POSIX_ESCALATION_BINARIES,
    SEE_MASK_NO_CONSOLE,
    SEE_MASK_NOCLOSEPROCESS,
    SW_HIDE,
    WAIT_FAILED,
    WAIT_TIMEOUT,
)

logger = get_logger(__name__)


class ElevationOutcome(enum.StrEnum):
    """How an elevation attempt finished."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


class ElevationRequest(BaseModel):
    """A command a module wants to run with elevated privileges.

    Attributes:
        executable: Program to run. Resolved against PATH when not absolute.
        arguments: Arguments passed to *executable*.
        reason: User-facing justification, shown in the confirmation dialog.
            Required — an elevation with no stated reason is a blind elevation.
        timeout_seconds: How long to wait for the command to finish.
    """

    executable: str = Field(min_length=1)
    arguments: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    timeout_seconds: int = Field(default=ELEVATION_TIMEOUT_SECONDS, gt=0)


class ElevationResult(BaseModel):
    """The result of an elevation attempt.

    Attributes:
        outcome: How the attempt finished.
        exit_code: Process exit code when it ran to completion, else ``None``.
        message: User-readable summary, safe to show in the UI.
    """

    outcome: ElevationOutcome
    exit_code: int | None = None
    message: str

    @property
    def ok(self) -> bool:
        """True only when the command ran and exited zero."""
        return self.outcome is ElevationOutcome.SUCCEEDED and self.exit_code == 0


def is_elevated() -> bool:
    """Return True when the current process already has admin/root rights.

    Returns:
        True if elevated. False on any detection failure — assuming the
        lesser privilege is always the safe default.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            # BOOL, not the implicit C int — see _elevate_windows for why the
            # default signatures are not safe to rely on here.
            shell32.IsUserAnAdmin.argtypes = ()
            shell32.IsUserAnAdmin.restype = ctypes.c_int
            return bool(shell32.IsUserAnAdmin())
        except (AttributeError, OSError) as exc:
            logger.warning("permission.admin_check_failed — %s", exc)
            return False
    return os.geteuid() == 0


def describe(request: ElevationRequest) -> str:
    """Return the exact command line that elevation would execute.

    Show this to the user verbatim before calling :func:`elevate` — rule B-05
    forbids elevating a command the user has not seen.

    Args:
        request: The elevation request to describe.

    Returns:
        A shell-quoted command line, e.g. ``sync && echo 3 > /proc/sys/vm/drop_caches``.
    """
    return shlex.join([request.executable, *request.arguments])


def find_escalation_binary() -> str | None:
    """Return the POSIX escalation helper to use, preferring polkit.

    Returns:
        Absolute path to ``pkexec`` or ``sudo``, or ``None`` when neither is
        installed.
    """
    for name in POSIX_ESCALATION_BINARIES:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    return None


def elevate(request: ElevationRequest) -> ElevationResult:
    """Run *request* with elevated privileges.

    The caller must already have shown :func:`describe` to the user and
    obtained consent.

    Args:
        request: The command to elevate.

    Returns:
        An :class:`ElevationResult`. Never raises for an ordinary failure —
        cancellation, timeout and a missing helper are all reported as
        outcomes so the UI can explain what happened.
    """
    logger.info(
        "permission.elevation_requested — executable=%s reason=%s",
        request.executable,
        request.reason,
    )

    if is_elevated():
        return _run_directly(request)
    if sys.platform == "win32":
        return _elevate_windows(request)
    return _elevate_posix(request)


def _run_directly(request: ElevationRequest) -> ElevationResult:
    """Run the command as-is because the process is already elevated."""
    try:
        completed = subprocess.run(
            [request.executable, *request.arguments],
            timeout=request.timeout_seconds,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ElevationResult(
            outcome=ElevationOutcome.TIMED_OUT,
            message=f"Command exceeded {request.timeout_seconds}s and was terminated.",
        )
    except OSError as exc:
        logger.error("permission.direct_run_failed", exc_info=exc)
        return ElevationResult(
            outcome=ElevationOutcome.FAILED, message=f"Could not run command: {exc}"
        )

    return _result_from_exit_code(completed.returncode, _decode(completed.stderr))


def _elevate_posix(request: ElevationRequest) -> ElevationResult:
    """Elevate via pkexec or sudo."""
    escalator = find_escalation_binary()
    if escalator is None:
        helpers = " or ".join(POSIX_ESCALATION_BINARIES)
        return ElevationResult(
            outcome=ElevationOutcome.UNAVAILABLE,
            message=f"No privilege escalation helper found. Install {helpers} and retry.",
        )

    try:
        completed = subprocess.run(
            [escalator, request.executable, *request.arguments],
            timeout=request.timeout_seconds,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ElevationResult(
            outcome=ElevationOutcome.TIMED_OUT,
            message=f"Command exceeded {request.timeout_seconds}s and was terminated.",
        )
    except OSError as exc:
        logger.error("permission.escalation_failed", exc_info=exc)
        return ElevationResult(
            outcome=ElevationOutcome.FAILED, message=f"Elevation failed to start: {exc}"
        )

    # Both pkexec and sudo use 126/127 to report an authentication refusal or
    # a command that could not be executed.
    if completed.returncode in (126, 127):
        return ElevationResult(
            outcome=ElevationOutcome.CANCELLED,
            exit_code=completed.returncode,
            message="Authorisation was declined or the command could not be run.",
        )
    return _result_from_exit_code(completed.returncode, _decode(completed.stderr))


def _elevate_windows(request: ElevationRequest) -> ElevationResult:
    """Elevate via ShellExecuteEx with the ``runas`` verb (triggers UAC).

    The ctypes plumbing is defined inside this function because
    ``ctypes.wintypes`` cannot even be imported on non-Windows platforms.
    """
    import ctypes
    from ctypes import wintypes

    class _ShellExecuteInfoW(ctypes.Structure):
        """Mirrors the Win32 SHELLEXECUTEINFOW structure."""

        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        )

    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    info.lpVerb = "runas"
    info.lpFile = request.executable
    info.lpParameters = subprocess.list2cmdline(request.arguments)
    info.nShow = SW_HIDE

    # use_last_error=True makes ctypes snapshot ShellExecuteExW's own error so
    # ctypes.get_last_error() below reads it, instead of a stale thread error
    # left by some unrelated ctypes call — the default ctypes.windll handles do
    # not capture it, which could mislabel a dismissed UAC prompt (RFC 0002).
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # ctypes defaults every argument and return value to signed C int, which is
    # wrong twice over on 64-bit Windows: a failed wait returns -1 and never
    # equals WAIT_FAILED (0xFFFFFFFF), leaving that branch unreachable, and a
    # pointer-sized HANDLE gets truncated to 32 bits on the way in. Declaring
    # the real signatures fixes both.
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == ERROR_CANCELLED:
            return ElevationResult(
                outcome=ElevationOutcome.CANCELLED,
                message="The administrator prompt was dismissed.",
            )
        logger.error("permission.shellexecute_failed — code=%d", code)
        return ElevationResult(
            outcome=ElevationOutcome.FAILED,
            message=f"Windows refused to elevate the command (error {code}).",
        )

    try:
        waited = kernel32.WaitForSingleObject(
            info.hProcess, request.timeout_seconds * 1000
        )
        if waited == WAIT_TIMEOUT:
            return ElevationResult(
                outcome=ElevationOutcome.TIMED_OUT,
                message=f"Command exceeded {request.timeout_seconds}s.",
            )
        if waited == WAIT_FAILED:
            return ElevationResult(
                outcome=ElevationOutcome.FAILED,
                message="Lost track of the elevated process.",
            )

        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            return ElevationResult(
                outcome=ElevationOutcome.FAILED,
                message="Could not read the elevated process exit code.",
            )
        return _result_from_exit_code(int(exit_code.value))
    finally:
        kernel32.CloseHandle(info.hProcess)


def _decode(raw: bytes | str | None) -> str:
    """Render captured stderr as text, whatever form subprocess returned it in.

    Args:
        raw: The captured stream, or ``None`` when nothing was captured.

    Returns:
        Decoded text, empty when there was none.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _result_from_exit_code(exit_code: int, stderr: str = "") -> ElevationResult:
    """Convert a process exit code into an ElevationResult.

    Args:
        exit_code: The command's exit status.
        stderr: What the command wrote to stderr, when it was captured. Its
            tail is appended to the failure message — "exited with code 1" on
            its own gives the user nothing to act on (rule E-04). This is the
            command's own diagnostic output, not application state, and it is
            surfaced in the UI rather than written to the log (rule C-04).

    Returns:
        The corresponding result.
    """
    if exit_code == 0:
        return ElevationResult(
            outcome=ElevationOutcome.SUCCEEDED,
            exit_code=0,
            message="Command completed successfully.",
        )

    message = f"Command exited with code {exit_code}."
    detail = " ".join(stderr.split())
    if detail:
        if len(detail) > ELEVATION_STDERR_MAX_CHARS:
            detail = detail[:ELEVATION_STDERR_MAX_CHARS] + "…"
        message = f"{message} {detail}"
    return ElevationResult(
        outcome=ElevationOutcome.FAILED,
        exit_code=exit_code,
        message=message,
    )


def resolve_executable(name: str) -> Path | None:
    """Resolve *name* to an absolute executable path.

    Args:
        name: Executable name or path.

    Returns:
        The resolved path, or ``None`` when it is not on PATH.
    """
    found = shutil.which(name)
    return Path(found) if found else None
