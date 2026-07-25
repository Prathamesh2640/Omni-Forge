"""Unit tests for core.logger (JSON formatting and file sink)."""
from __future__ import annotations

import datetime
import json
import logging

from core.logger import JsonFormatter, get_logger, log_file_path
from shared.constants import LOG_RETENTION_DAYS, LOG_ROOT_NAME, LOG_ROTATION_WHEN


def _record(**kwargs: object) -> logging.LogRecord:
    """Build a LogRecord, applying *kwargs* as structured extras."""
    record = logging.LogRecord(
        name="omniforge.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="operation.completed",
        args=(),
        exc_info=None,
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


def test_formats_record_as_single_line_json() -> None:
    output = JsonFormatter().format(_record())
    assert "\n" not in output
    assert json.loads(output)["message"] == "operation.completed"


def test_includes_the_mandatory_fields() -> None:
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "omniforge.test"
    assert "timestamp" in payload


def test_timestamp_is_iso_8601_parsable() -> None:
    payload = json.loads(JsonFormatter().format(_record()))
    assert isinstance(
        datetime.datetime.fromisoformat(payload["timestamp"]), datetime.datetime
    )


def test_structured_extras_are_promoted_to_top_level_keys() -> None:
    payload = json.loads(
        JsonFormatter().format(
            _record(module_id="extractors.llm_packager", operation="pack",
                    duration_ms=1234, status="ok")
        )
    )
    assert payload["module_id"] == "extractors.llm_packager"
    assert payload["operation"] == "pack"
    assert payload["duration_ms"] == 1234
    assert payload["status"] == "ok"


def test_unset_structured_fields_are_omitted() -> None:
    payload = json.loads(JsonFormatter().format(_record(operation="scan")))
    assert payload["operation"] == "scan"
    assert "duration_ms" not in payload
    assert "status" not in payload


def test_arbitrary_extras_are_not_leaked_into_the_log() -> None:
    """Only the declared field allow-list is serialised (rule C-04)."""
    payload = json.loads(JsonFormatter().format(_record(secret_token="sk-live-abc123")))
    assert "secret_token" not in payload


def test_exception_is_rendered_into_the_exception_field() -> None:
    try:
        raise ValueError("something broke")
    except ValueError as exc:
        record = _record()
        record.exc_info = (type(exc), exc, exc.__traceback__)

    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: something broke" in payload["exception"]


def test_log_file_path_is_a_fixed_name() -> None:
    """Dating belongs to the rotating handler, not to the filename.

    Doing both made the two mechanisms fight: the handler kept writing to a
    base file named for the day the *session started*, so a run spanning
    midnight filed the new day's records under the old date (RFC 0005).
    """
    path = log_file_path()
    assert path.name == "omniforge.log"
    # Absolute and CWD-independent, anchored under the app root's data/logs.
    assert path.is_absolute()
    assert path.parent.name == "logs"
    assert path.parent.parent.name == "data"


def test_log_file_path_ignores_the_legacy_date_argument() -> None:
    """The parameter is retained for existing callers but no longer used."""
    assert log_file_path(datetime.date(2026, 7, 22)) == log_file_path()


def test_rotation_is_configured_to_prune_a_real_window() -> None:
    """The handler owns rollover, so backupCount spans days, not sessions."""
    assert LOG_ROTATION_WHEN == "midnight"
    assert LOG_RETENTION_DAYS == 14


def test_get_logger_is_namespaced_under_the_root() -> None:
    assert get_logger("core.demo").name == f"{LOG_ROOT_NAME}.core.demo"


def test_root_logger_has_stderr_and_file_handlers() -> None:
    get_logger("core.demo")
    handlers = logging.getLogger(LOG_ROOT_NAME).handlers
    assert any(isinstance(h, logging.FileHandler) for h in handlers)
    assert any(type(h) is logging.StreamHandler for h in handlers)


def test_repeated_get_logger_calls_do_not_duplicate_handlers() -> None:
    get_logger("core.one")
    before = len(logging.getLogger(LOG_ROOT_NAME).handlers)
    get_logger("core.two")
    assert len(logging.getLogger(LOG_ROOT_NAME).handlers) == before
