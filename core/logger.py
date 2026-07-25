"""Centralised structured logger for OmniForge.

All modules obtain a logger via ``get_logger(__name__)``.

Two handlers are attached to the ``omniforge`` root logger:
- stderr — human-readable text, for development.
- file   — one JSON object per line, at ``data/logs/omniforge-<date>.log``.

Structured fields (``module_id``, ``operation``, ``duration_ms``, ``status``)
are picked up from the record when passed via ``extra=``::

    logger.info("pdf_suite.merge", extra={"operation": "merge", "status": "ok"})

Log content policy (rule C-04):
- Allowed: operation type, timestamp, path (optional), status, error codes.
- Forbidden: file contents, secrets, personal data.
"""
from __future__ import annotations

import datetime
import json
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from shared.constants import (
    LOG_DIR,
    LOG_FILENAME,
    LOG_RETENTION_DAYS,
    LOG_ROOT_NAME,
    LOG_ROTATION_WHEN,
    LOG_STRUCTURED_FIELDS,
)
from shared.platform_info import ensure_utf8_streams

_LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

_configured: bool = False


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object.

    Emits ``timestamp``, ``level``, ``logger`` and ``message`` always, plus
    any of :data:`~shared.constants.LOG_STRUCTURED_FIELDS` present on the
    record. Exceptions are rendered into an ``exception`` field.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialise *record* to a JSON string.

        Args:
            record: The log record to format.

        Returns:
            A single-line JSON document (no trailing newline).
        """
        created = datetime.datetime.fromtimestamp(record.created, tz=datetime.UTC)
        payload: dict[str, Any] = {
            "timestamp": created.isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in LOG_STRUCTURED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def log_file_path(today: datetime.date | None = None) -> Path:
    """Return the path of the live log file.

    The name is fixed. Dating is the rotating handler's job — it renames this
    file to ``omniforge.log.<date>`` at midnight — so building a date into the
    name here would leave the live file named for whenever the session began
    (RFC 0005).

    Args:
        today: Accepted and ignored; kept so existing callers still work.

    Returns:
        Path to ``data/logs/omniforge.log``.
    """
    del today
    return Path(LOG_DIR) / LOG_FILENAME


def _configure_root() -> None:
    """Attach the stderr and JSON file handlers to the root logger once."""
    global _configured
    if _configured:
        return
    _configured = True

    # The log format and messages contain — → … ⚒; a legacy Windows console
    # (cp1252) would raise UnicodeEncodeError on every line without this.
    ensure_utf8_streams()

    root = logging.getLogger(LOG_ROOT_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
    stderr_handler.setLevel(logging.DEBUG)
    root.addHandler(stderr_handler)

    # A read-only or missing data/ directory must never prevent the app from
    # starting — the stderr handler alone is a valid fallback.
    try:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The handler owns rotation *and* dating: it writes to a fixed
        # omniforge.log and renames it to omniforge.log.<date> at midnight, so
        # backupCount prunes a genuine 14-day window. See RFC 0003 and 0005.
        file_handler = TimedRotatingFileHandler(
            path, when=LOG_ROTATION_WHEN, backupCount=LOG_RETENTION_DAYS, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("logger.file_handler_unavailable — %s", exc)


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for the given module name.

    Args:
        name: Typically ``__name__`` of the calling module. The returned
              logger is a child of the ``omniforge`` root logger.

    Returns:
        A Logger instance attached to the OmniForge logging tree.
    """
    _configure_root()
    return logging.getLogger(f"{LOG_ROOT_NAME}.{name}")
