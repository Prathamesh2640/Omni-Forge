"""Unit tests for core.storage (thread-safe TinyDB wrapper)."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from core import storage

_TABLE = "test_module"


@pytest.fixture(autouse=True)
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the storage module at a throwaway database for each test.

    ``storage.reset()`` is what actually isolates the test: the module keeps a
    process-global in-memory mirror, so redirecting ``_DB_PATH`` alone would
    leave the previously loaded contents (including the real application
    database's) visible to this test.
    """
    storage.reset()
    monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "test.db")
    yield
    storage.reset()


def test_get_returns_default_when_key_absent() -> None:
    assert storage.store_get(_TABLE, "missing", default="fallback") == "fallback"


def test_get_default_is_none_when_unspecified() -> None:
    assert storage.store_get(_TABLE, "missing") is None


def test_set_then_get_round_trips() -> None:
    storage.store_set(_TABLE, "last_dir", "/home/user/docs")
    assert storage.store_get(_TABLE, "last_dir") == "/home/user/docs"


@pytest.mark.parametrize(
    "value",
    [42, 3.14, True, None, ["a", "b"], {"nested": {"k": 1}}],
    ids=["int", "float", "bool", "none", "list", "dict"],
)
def test_round_trips_json_serialisable_types(value: object) -> None:
    storage.store_set(_TABLE, "key", value)
    assert storage.store_get(_TABLE, "key") == value


def test_set_overwrites_existing_key() -> None:
    storage.store_set(_TABLE, "theme", "dark")
    storage.store_set(_TABLE, "theme", "cyberpunk")
    assert storage.store_get(_TABLE, "theme") == "cyberpunk"


def test_tables_are_isolated_from_each_other() -> None:
    storage.store_set("module_a", "key", "a-value")
    storage.store_set("module_b", "key", "b-value")
    assert storage.store_get("module_a", "key") == "a-value"
    assert storage.store_get("module_b", "key") == "b-value"


def test_delete_removes_the_key() -> None:
    storage.store_set(_TABLE, "temp", "value")
    storage.store_delete(_TABLE, "temp")
    assert storage.store_get(_TABLE, "temp", default="gone") == "gone"


def test_delete_of_absent_key_is_silent() -> None:
    storage.store_delete(_TABLE, "never_existed")


def test_concurrent_writes_do_not_corrupt_the_database() -> None:
    """Every writer's key must survive parallel access (the threading.Lock contract)."""
    writer_count = 8
    writes_per_thread = 20

    def writer(thread_id: int) -> None:
        for i in range(writes_per_thread):
            storage.store_set(_TABLE, f"t{thread_id}_k{i}", i)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(writer_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for thread_id in range(writer_count):
        for i in range(writes_per_thread):
            assert storage.store_get(_TABLE, f"t{thread_id}_k{i}") == i


# ─── Write-behind cache (RFC 0005) ─────────────────────────────────────────────


class TestWriteBehind:
    """Writes are deferred to a thread, but must never be observably deferred.

    TinyDB rewrites the whole file per write — 3.4 ms measured — and that used
    to land on the GUI event loop once per keystroke in a path field.
    """

    def test_a_write_is_visible_to_the_next_read_immediately(self) -> None:
        storage.store_set(_TABLE, "k", "v")
        # No flush: the mirror is the read path, so the deferral is invisible.
        assert storage.store_get(_TABLE, "k") == "v"

    def test_a_delete_is_visible_to_the_next_read_immediately(self) -> None:
        storage.store_set(_TABLE, "k", "v")
        storage.store_delete(_TABLE, "k")
        assert storage.store_get(_TABLE, "k", default="gone") == "gone"

    def test_writes_do_not_touch_the_disk(self) -> None:
        """The event loop must not pay for a database write.

        The mirror is populated by one lazy read on first access — that open
        does hit the disk, once per process. What must never hit it is the
        *writes*, which is the per-keystroke cost the cache exists to remove.
        """
        storage.store_get(_TABLE, "prime")  # load the mirror
        storage.flush()
        before = storage._DB_PATH.stat().st_mtime_ns

        for index in range(20):
            storage.store_set(_TABLE, "path", f"C:/dir{index}")

        assert storage._DB_PATH.stat().st_mtime_ns == before

    def test_flush_puts_the_value_on_disk(self) -> None:
        storage.store_set(_TABLE, "k", "v")

        assert storage.flush() is True

        assert storage._DB_PATH.exists()
        raw = json.loads(storage._DB_PATH.read_text(encoding="utf-8"))
        stored = [row for row in raw[_TABLE].values() if row["key"] == "k"]
        assert stored and stored[0]["value"] == "v"

    def test_a_flushed_delete_is_removed_from_disk(self) -> None:
        storage.store_set(_TABLE, "keep", 1)
        storage.store_set(_TABLE, "drop", 2)
        storage.flush()

        storage.store_delete(_TABLE, "drop")
        storage.flush()

        raw = json.loads(storage._DB_PATH.read_text(encoding="utf-8"))
        keys = {row["key"] for row in raw[_TABLE].values()}
        assert keys == {"keep"}

    def test_a_burst_of_writes_survives_as_the_last_value(self) -> None:
        """A typed path fires one event per character; only the result matters."""
        for index in range(50):
            storage.store_set(_TABLE, "path", f"C:/some/dir{index}")
        storage.flush()

        assert storage.store_get(_TABLE, "path") == "C:/some/dir49"
        raw = json.loads(storage._DB_PATH.read_text(encoding="utf-8"))
        rows = [row for row in raw[_TABLE].values() if row["key"] == "path"]
        assert len(rows) == 1  # coalesced, not 50 accumulated rows

    def test_values_survive_a_reload_from_disk(self) -> None:
        storage.store_set(_TABLE, "k", {"nested": [1, 2]})
        storage.flush()
        # Drop the mirror but keep the same file, as a restart would.
        storage._cache = None

        assert storage.store_get(_TABLE, "k") == {"nested": [1, 2]}

    def test_flush_is_idempotent_when_nothing_is_pending(self) -> None:
        assert storage.flush() is True
        assert storage.flush() is True

    def test_reset_isolates_a_newly_pointed_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repointing the store must not carry the old rows into the new file.

        This is the guarantee the test fixtures depend on: the mirror is
        process-global, so without a reset it would survive a ``_DB_PATH``
        change and show one database's contents through another.
        """
        storage.store_set(_TABLE, "k", "v")
        storage.flush()

        storage.reset()
        monkeypatch.setattr(storage, "_DB_PATH", tmp_path / "elsewhere.db")

        assert storage.store_get(_TABLE, "k") is None

    def test_a_corrupt_database_does_not_stop_the_app(self) -> None:
        storage._DB_PATH.write_text("{ this is not json", encoding="utf-8")
        storage.reset()

        assert storage.store_get(_TABLE, "anything", default="fallback") == "fallback"


class TestFlushIsNotDestructive:
    """A flush must only ever remove keys the caller actually deleted.

    Inferring deletions as "on disk but not in the mirror" makes every flush
    destructive whenever the mirror is an incomplete picture of the file — and
    it legitimately is after a load that failed or was reset. This wiped a real
    setting (the saved theme) during development, so it is pinned here.
    """

    def test_a_partial_mirror_does_not_delete_the_rest_of_the_table(self) -> None:
        """The exact shape that lost the saved theme.

        ``_load()`` returns an empty mirror when the database cannot be read,
        and a later write then makes that table dirty while the mirror knows
        about only the one key just written. Inferring deletions from that
        picture removes every *other* key the user had.
        """
        storage.store_set(_TABLE, "theme", "cyberpunk")
        storage.store_set(_TABLE, "recent", ["a", "b"])
        storage.flush()

        # An empty dict, not None: this is a mirror that has already "loaded"
        # and simply knows nothing, exactly as a failed load leaves it.
        storage._cache = {}
        storage.store_set(_TABLE, "recent", ["c"])
        storage.flush()

        raw = json.loads(storage._DB_PATH.read_text(encoding="utf-8"))
        stored = {row["key"]: row["value"] for row in raw[_TABLE].values()}
        assert stored["theme"] == "cyberpunk"  # the setting that was destroyed
        assert stored["recent"] == ["c"]

    def test_an_unrelated_tables_write_leaves_this_one_alone(self) -> None:
        storage.store_set(_TABLE, "keep_me", 1)
        storage.flush()

        storage._cache = {}
        storage.store_set("unrelated_table", "k", "v")
        storage.flush()

        raw = json.loads(storage._DB_PATH.read_text(encoding="utf-8"))
        stored = {row["key"] for row in raw[_TABLE].values()}
        assert stored == {"keep_me"}

    def test_an_explicit_delete_still_removes_the_row(self) -> None:
        storage.store_set(_TABLE, "gone", 1)
        storage.store_set(_TABLE, "stays", 2)
        storage.flush()

        storage.store_delete(_TABLE, "gone")
        storage.flush()

        raw = json.loads(storage._DB_PATH.read_text(encoding="utf-8"))
        stored = {row["key"] for row in raw[_TABLE].values()}
        assert stored == {"stays"}
