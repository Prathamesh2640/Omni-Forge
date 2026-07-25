"""Thread-safe persistent store for module state, backed by TinyDB.

Reads come from an in-memory mirror; writes are coalesced and flushed to disk
by a single background thread. TinyDB is not thread-safe (rule D-note), and it
rewrites the whole database file on every write — measured at 3.4 ms on a
development machine. That cost used to land on the GUI event loop, once per
``store_set``, which includes once per *keystroke* in the output-directory
picker. Doing the I/O on a writer thread keeps the loop free, and coalescing
turns a burst of keystrokes into a single write (see RFC 0005).

The mirror is authoritative for reads, so a ``store_get`` immediately after a
``store_set`` always sees the new value — callers cannot observe the deferral.
What they can lose is up to :data:`STORAGE_FLUSH_DEBOUNCE_SECONDS` of writes if
the process is killed outright. Everything kept here is a user *preference*
(last-used directory, theme, recent modules); module output is files on disk and
the recycle store has its own manifest, so nothing irreplaceable rides on it.
:func:`flush` makes a clean shutdown lossless.

Usage::

    from core.storage import store_get, store_set

    store_set("my_module", "last_dir", "/home/user/docs")
    value = store_get("my_module", "last_dir", default="/")
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from tinydb import Query, TinyDB

from core.logger import get_logger
from shared.constants import (
    DATA_DIR,
    STORAGE_FLUSH_DEBOUNCE_SECONDS,
    STORAGE_FLUSH_JOIN_SECONDS,
)

logger = get_logger(__name__)

# Anchored to the app root (DATA_DIR is absolute), so the database is not
# scattered across whatever directory the app was launched from. See RFC 0002.
_DB_PATH = Path(DATA_DIR) / "omniforge.db"

#: Guards the mirror and the dirty set. Held only for dictionary operations —
#: never across disk I/O, which is the whole point of the rewrite.
_lock = threading.Lock()

#: table → key → value. ``None`` until loaded from disk on first access.
_cache: dict[str, dict[str, Any]] | None = None

#: Tables changed since the last successful flush.
_dirty: set[str] = set()

#: table → keys explicitly deleted since the last flush. Deletions are tracked
#: rather than inferred from the mirror, so a table the mirror only partly
#: knows about can never have the rest of its rows removed.
_deleted: dict[str, set[str]] = {}

#: Signalled when there is something to write; also wakes the writer to exit.
_work = threading.Event()

#: Set once the mirror is on disk and nothing is pending.
_idle = threading.Event()
_idle.set()

#: Asks the writer thread to finish. Also makes its debounce interruptible, so
#: a shutdown never waits out a delay whose only purpose was coalescing.
_stopping = threading.Event()

_writer: threading.Thread | None = None
_db: TinyDB | None = None


def _get_db() -> TinyDB:
    """Open the database, creating its directory on first use.

    Only ever called from the writer thread (or from :func:`flush` once the
    writer has been joined), so TinyDB is never touched concurrently.

    Returns:
        The initialised TinyDB database object.
    """
    global _db
    if _db is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = TinyDB(_DB_PATH)
        logger.debug("storage.init — path=%s", _DB_PATH)
    return _db


def _load() -> dict[str, dict[str, Any]]:
    """Read the whole database into the mirror.

    Called once, lazily, under :data:`_lock`. The database holds a handful of
    preference rows, so reading it whole is cheaper than the per-key queries it
    replaces.

    Returns:
        The loaded mirror, empty when the file is absent or unreadable.
    """
    mirror: dict[str, dict[str, Any]] = {}
    try:
        db = _get_db()
        for table_name in db.tables():
            table = db.table(table_name)
            mirror[table_name] = {
                str(row["key"]): row["value"]
                for row in table
                if "key" in row and "value" in row
            }
    except (OSError, ValueError) as exc:
        # A missing or corrupt database must not stop the app from starting —
        # the user loses their preferences, not their session.
        logger.warning("storage.load_failed — path=%s reason=%s", _DB_PATH, exc)
        return {}
    return mirror


def _mirror() -> dict[str, dict[str, Any]]:
    """Return the in-memory mirror, loading it on first use.

    Callers must already hold :data:`_lock`.

    Returns:
        The mirror dictionary.
    """
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def _ensure_writer() -> None:
    """Start the background writer thread if it is not already running.

    Callers must already hold :data:`_lock`.
    """
    global _writer
    if _writer is not None and _writer.is_alive():
        return
    _writer = threading.Thread(target=_writer_loop, name="omniforge-storage", daemon=True)
    _writer.start()


def _writer_loop() -> None:
    """Flush dirty tables to disk, coalescing bursts of writes.

    Waiting out the debounce before each flush is what turns a keystroke burst
    into one database write instead of one per character.
    """
    while not _stopping.is_set():
        _work.wait()
        if _stopping.is_set():
            return
        _work.clear()
        # Let a burst settle before paying for a write; cut short on shutdown.
        _stopping.wait(STORAGE_FLUSH_DEBOUNCE_SECONDS)
        _write_pending()


def _write_pending() -> None:
    """Write every dirty table to disk, then mark the store idle.

    Snapshots the pending work under the lock, then does the actual I/O outside
    it, so no reader or writer is ever blocked on disk.

    Only keys the caller actually wrote are upserted, and only keys the caller
    actually deleted are removed. Inferring the deletions instead — "on disk
    but not in the mirror" — makes the flush destructive whenever the mirror is
    an incomplete picture of the file, which it legitimately is after a failed
    or partial load. That mistake wiped real settings during development.
    """
    with _lock:
        if not _dirty and not _deleted:
            _idle.set()
            return
        pending = set(_dirty)
        _dirty.clear()
        snapshot = {name: dict(_mirror().get(name, {})) for name in pending}
        removals = {name: set(keys) for name, keys in _deleted.items()}
        _deleted.clear()

    try:
        db = _get_db()
        for table_name, rows in snapshot.items():
            table = db.table(table_name)
            query = Query()
            for key, value in rows.items():
                table.upsert({"key": key, "value": value}, query.key == key)
        for table_name, keys in removals.items():
            table = db.table(table_name)
            query = Query()
            for key in keys:
                table.remove(query.key == key)
        logger.debug(
            "storage.flushed — tables=%d removals=%d", len(snapshot), len(removals)
        )
    except (OSError, ValueError) as exc:
        # Put the work back so the next flush retries it rather than silently
        # dropping the user's settings.
        logger.error("storage.flush_failed — path=%s", _DB_PATH, exc_info=exc)
        with _lock:
            _dirty.update(snapshot)
            for name, keys in removals.items():
                _deleted.setdefault(name, set()).update(keys)
        return

    with _lock:
        if not _dirty and not _deleted:
            _idle.set()


def _mark_dirty(table: str) -> None:
    """Record that *table* needs writing and wake the writer.

    Callers must already hold :data:`_lock`.

    Args:
        table: The table that changed.
    """
    _dirty.add(table)
    _idle.clear()
    _ensure_writer()
    _work.set()


def store_set(table: str, key: str, value: Any) -> None:
    """Upsert a key-value pair in *table*.

    Returns as soon as the in-memory mirror is updated; the disk write happens
    on the writer thread. A subsequent :func:`store_get` sees the new value
    immediately.

    Args:
        table: TinyDB table name — typically the module's ``module_id``.
        key: String key within the table.
        value: JSON-serialisable value.
    """
    with _lock:
        _mirror().setdefault(table, {})[key] = value
        _mark_dirty(table)
    logger.debug("storage.set — table=%s key=%s", table, key)


def store_get(table: str, key: str, default: Any = None) -> Any:
    """Retrieve a value from *table* by *key*.

    Args:
        table: TinyDB table name.
        key: String key to look up.
        default: Returned when the key is absent.

    Returns:
        Stored value or *default*.
    """
    with _lock:
        return _mirror().get(table, {}).get(key, default)


def store_delete(table: str, key: str) -> None:
    """Remove a key-value pair from *table*.

    A missing key is silently ignored.

    Args:
        table: TinyDB table name.
        key: String key to remove.
    """
    with _lock:
        if _mirror().get(table, {}).pop(key, None) is None:
            return
        _deleted.setdefault(table, set()).add(key)
        _mark_dirty(table)
    logger.debug("storage.delete — table=%s key=%s", table, key)


def flush(timeout: float = STORAGE_FLUSH_JOIN_SECONDS) -> bool:
    """Block until every pending write has reached disk.

    Call from the application's shutdown hook so a clean exit never loses a
    preference. Also used by tests that need to assert on the file itself.

    Args:
        timeout: Seconds to wait for the writer to finish.

    Returns:
        True when the store is idle, False if *timeout* elapsed first.
    """
    with _lock:
        pending = bool(_dirty or _deleted)
    if pending:
        # Write inline rather than waiting out the writer's debounce — shutdown
        # should not pay for a delay whose only purpose is coalescing.
        _write_pending()
    return _idle.wait(timeout)


def _stop_writer() -> None:
    """Stop the writer thread and wait for it to finish.

    Callers must not hold :data:`_lock` — the writer takes it.
    """
    global _writer
    writer = _writer
    if writer is None or not writer.is_alive():
        _writer = None
        return
    _stopping.set()
    _work.set()
    writer.join(timeout=STORAGE_FLUSH_JOIN_SECONDS)
    _stopping.clear()
    _work.clear()
    _writer = None


def reset() -> None:
    """Discard all in-memory state and close the database.

    The mirror is process-global, so it necessarily outlives any redirection of
    :data:`_DB_PATH`. Tests that point the store at a throwaway database must
    call this, or the previous database's contents leak into the next one.

    Pending writes are **dropped**, not flushed — a test repointing the store
    does not want the previous test's rows arriving in its file. The writer
    thread is stopped first for the same reason: a flush already in flight
    would otherwise finish against whichever database is current by then.
    """
    global _cache, _db
    _stop_writer()
    with _lock:
        _cache = None
        _dirty.clear()
        _deleted.clear()
        _idle.set()
        if _db is not None:
            _db.close()
            _db = None


# Deliberately NOT registered with atexit. An exit hook flushes the mirror to
# whatever ``_DB_PATH`` happens to be at interpreter shutdown — and in a test
# process that is the *real* application database, once the fixture's
# monkeypatch has unwound. That is how a test's rows end up in the user's
# settings. ``app._on_shutdown`` calls :func:`flush` explicitly instead, which
# is the only place that knows it is talking about the real database.
