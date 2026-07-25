"""Guards against terminating processes OmniForge depends on to keep running.

Both ``process_manager`` (kill a process) and ``port_monitor`` (free a port by
stopping its owner) can be pointed at any PID on the machine, including the
application's own. Modules may not import one another (rule A-03), so the shared
guard lives here, in the lowest layer both are allowed to depend on (rule A-07).

Checking ``os.getpid()`` alone is not enough. In native mode NiceGUI runs
pywebview in a **separate** process, and ``core.sandbox`` spawns process-pool
workers as children — killing any of those takes the application down while the
guard cheerfully reports success. The whole process tree is therefore off
limits, along with the operating system's own critical PIDs.
"""
from __future__ import annotations

import logging
import os

import psutil

from shared.constants import (
    LOG_ROOT_NAME,
    POSIX_INIT_PID,
    WINDOWS_CRITICAL_PIDS,
)
from shared.platform_info import is_windows

logger = logging.getLogger(f"{LOG_ROOT_NAME}.shared.process_guard")


def related_pids() -> set[int]:
    """Return every PID whose death would take this application down.

    That is: this process, everything it spawned (pywebview in native mode,
    sandbox pool workers, any subprocess a module started), and its **immediate**
    parent — the launcher that owns the window.

    The whole ancestor chain is deliberately *not* included. ``parents()`` walks
    all the way up, which on Windows reaches the terminal, ``explorer.exe`` and
    ultimately the session leader. Guarding those made Process Manager refuse to
    kill the user's own file manager or shell — ordinary things to want to kill
    — while telling them the process "belongs to OmniForge", which is both wrong
    and unhelpful. Killing a distant ancestor does not stop this process; only
    the direct parent genuinely owns our lifetime.

    Returns:
        The PIDs that must never be terminated. Degrades to just the current
        PID if the tree cannot be walked — the guard should narrow, not fail.
    """
    own = os.getpid()
    related = {own}
    try:
        process = psutil.Process(own)
        parent = process.parent()
        if parent is not None:
            related.add(parent.pid)
        related.update(child.pid for child in process.children(recursive=True))
    except Exception as exc:
        # Deliberately broad: this guard sits in front of every kill, and a
        # psutil quirk on some platform must degrade the protection to "our own
        # PID" rather than break process management entirely.
        logger.debug("process_guard.tree_walk_failed — reason=%s", exc)
    return related


def protected_pid_reason(pid: int, verb: str = "kill") -> str | None:
    """Return why *pid* must not be terminated, or ``None`` when it is fair game.

    Args:
        pid: The candidate process ID.
        verb: How the caller describes the action, e.g. ``"stop"``, so the
            refusal reads naturally in that module's UI.

    Returns:
        A user-facing refusal reason, or ``None`` to allow the operation.
    """
    own = os.getpid()
    if pid == own:
        return f"Refusing to {verb} OmniForge's own process."
    if pid in related_pids():
        return (
            f"Refusing to {verb} PID {pid} — it belongs to OmniForge "
            "(the application window or one of its workers), and stopping it "
            "would take the app down with it."
        )
    if is_windows():
        if pid in WINDOWS_CRITICAL_PIDS:
            return f"Refusing to {verb} a critical Windows system process (PID {pid})."
        return None
    if pid == POSIX_INIT_PID:
        return f"Refusing to {verb} PID 1 (init) — this would crash the system."
    return None
