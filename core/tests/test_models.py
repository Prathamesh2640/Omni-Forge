"""Unit tests for core.models (shared EventBus payload contracts)."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.models import (
    ModuleDegradedEvent,
    ModuleLoadedEvent,
    ProgressEvent,
)


class TestProgressEvent:
    def test_minimal_event_defaults_optional_fields(self) -> None:
        event = ProgressEvent(percent=0, message="Starting")
        assert event.output_path is None
        assert event.error is None
        assert event.cancelled is False

    @pytest.mark.parametrize("percent", [0, 50, 100])
    def test_accepts_the_full_valid_range(self, percent: int) -> None:
        assert ProgressEvent(percent=percent, message="ok").percent == percent

    @pytest.mark.parametrize("percent", [-1, 101])
    def test_rejects_out_of_range_percent(self, percent: int) -> None:
        with pytest.raises(ValidationError):
            ProgressEvent(percent=percent, message="out of range")

    def test_message_is_required(self) -> None:
        with pytest.raises(ValidationError):
            ProgressEvent(percent=0)  # type: ignore[call-arg]

    def test_completion_event_carries_the_output_path(self) -> None:
        event = ProgressEvent(
            percent=100, message="Done", output_path=Path("exports/result.txt")
        )
        assert event.output_path == Path("exports/result.txt")


class TestRegistryEvents:
    def test_loaded_event_carries_identity_fields(self) -> None:
        event = ModuleLoadedEvent(
            module_id="extractors.llm_packager",
            name="LLM Context Packager",
            pillar="extractors",
        )
        assert event.pillar == "extractors"

    def test_degraded_event_carries_the_reason(self) -> None:
        event = ModuleDegradedEvent(module_id="converters.media_suite", reason="ffmpeg missing")
        assert event.reason == "ffmpeg missing"
