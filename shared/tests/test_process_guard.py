"""Unit tests for shared.process_guard (§3.12g).

Checking ``os.getpid()`` alone was not enough: in native mode NiceGUI runs
pywebview in a separate process and ``core.sandbox`` spawns pool workers as
children, so a user could stop the application from inside it while the guard
reported success.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import psutil
import pytest

from shared import process_guard
from shared.process_guard import protected_pid_reason, related_pids


def _fake_tree(
    monkeypatch: pytest.MonkeyPatch, parents: list[int], children: list[int]
) -> None:
    """Make the current process appear to have the given relatives.

    *parents* is the full ancestor chain, nearest first, as ``psutil`` reports
    it. Only its head is the direct parent — the guard deliberately ignores the
    rest (see :func:`~shared.process_guard.related_pids`).
    """
    monkeypatch.setattr(
        process_guard.psutil,
        "Process",
        lambda _pid: SimpleNamespace(
            parent=lambda: SimpleNamespace(pid=parents[0]) if parents else None,
            parents=lambda: [SimpleNamespace(pid=p) for p in parents],
            children=lambda recursive=False: [SimpleNamespace(pid=c) for c in children],
        ),
    )


class TestRelatedPids:
    def test_always_includes_the_current_process(self) -> None:
        assert os.getpid() in related_pids()

    def test_includes_the_direct_parent_and_all_descendants(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_tree(monkeypatch, parents=[500], children=[900, 901])
        assert related_pids() == {os.getpid(), 500, 900, 901}

    def test_excludes_grandparents_and_beyond(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guarding the whole chain reached the shell, explorer.exe and init.

        Process Manager then refused to kill the user's own file manager while
        claiming it "belongs to OmniForge" — wrong, and it blocked a legitimate
        action. Only the direct parent owns this process's lifetime.
        """
        _fake_tree(monkeypatch, parents=[500, 400, 300, 1], children=[])

        guarded = related_pids()

        assert 500 in guarded  # the launcher that owns our window
        assert guarded.isdisjoint({400, 300, 1})  # the shell, explorer, init

    def test_a_broken_tree_walk_degrades_to_the_current_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must never break the operation it protects."""

        def boom(_pid: int) -> None:
            raise psutil.AccessDenied(_pid)

        monkeypatch.setattr(process_guard.psutil, "Process", boom)
        assert related_pids() == {os.getpid()}


class TestProtectedPidReason:
    def test_own_process_is_refused(self) -> None:
        reason = protected_pid_reason(os.getpid())
        assert reason is not None
        assert "own process" in reason

    def test_the_native_window_process_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pywebview runs as a child — killing it takes the app down."""
        _fake_tree(monkeypatch, parents=[], children=[4242])

        reason = protected_pid_reason(4242)

        assert reason is not None
        assert "belongs to OmniForge" in reason

    def test_the_launching_parent_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_tree(monkeypatch, parents=[7], children=[])
        assert protected_pid_reason(7) is not None

    def test_a_distant_ancestor_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Killing the user's shell or file manager is their call to make."""
        _fake_tree(monkeypatch, parents=[7, 6, 5], children=[])
        monkeypatch.setattr(process_guard, "is_windows", lambda: True)

        assert protected_pid_reason(6) is None
        assert protected_pid_reason(5) is None

    def test_an_unrelated_pid_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_tree(monkeypatch, parents=[7], children=[8])
        monkeypatch.setattr(process_guard, "is_windows", lambda: True)
        assert protected_pid_reason(31337) is None

    @pytest.mark.parametrize("pid", [0, 4])
    def test_windows_critical_pids_are_refused(
        self, monkeypatch: pytest.MonkeyPatch, pid: int
    ) -> None:
        _fake_tree(monkeypatch, parents=[], children=[])
        monkeypatch.setattr(process_guard, "is_windows", lambda: True)
        reason = protected_pid_reason(pid)
        assert reason is not None
        assert "critical Windows system process" in reason

    def test_posix_init_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_tree(monkeypatch, parents=[], children=[])
        monkeypatch.setattr(process_guard, "is_windows", lambda: False)
        reason = protected_pid_reason(1)
        assert reason is not None
        assert "init" in reason

    def test_the_verb_reaches_the_message(self) -> None:
        """Each module phrases the action its own way."""
        reason = protected_pid_reason(os.getpid(), verb="stop")
        assert reason is not None
        assert "stop" in reason
