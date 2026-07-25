"""Unit tests for llm_packager logic layer.

Coverage targets (rule D-02 -- 100% branch coverage on logic.py):
- _scan_files: include/exclude filtering, extension filtering
- _safe_read: success, too large, permission error
- _count_tokens: valid model, invalid model fallback
- _write_output: creates output file with correct content
- execute(): full happy path with all progress events
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from modules.extractors.llm_packager.constants import (
    APPROX_CHARS_PER_TOKEN,
    MAX_FILE_SIZE_BYTES,
    PROGRESS_WRITE_DONE,
    TIKTOKEN_CACHE_DIRNAME,
    TIKTOKEN_CACHE_ENV_VARS,
)
from modules.extractors.llm_packager.logic import (
    LLMPackagerLogic,
    tiktoken_cache_dir,
    vocabulary_is_available,
)
from modules.extractors.llm_packager.models import PackagedFile, PackageParams

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def logic() -> LLMPackagerLogic:
    """Fresh LLMPackagerLogic instance with no EventBus subscriptions."""
    return LLMPackagerLogic()


@pytest.fixture()
def temp_project(tmp_path: Path) -> Path:
    """Create a minimal fake project directory for testing.

    Returns:
        Root directory with sample source files.
    """
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "utils.ts").write_text("export const x = 1;\n", encoding="utf-8")
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "helper.py").write_text("def helper(): pass\n", encoding="utf-8")
    # File that should be excluded
    node = tmp_path / "node_modules"
    node.mkdir()
    (node / "excluded.js").write_text("// excluded", encoding="utf-8")
    return tmp_path


def _make_mock_encoder(token_count: int = 5) -> MagicMock:
    """Return a mock tiktoken encoder that avoids network calls.

    Args:
        token_count: Number of tokens the mock encode() returns.

    Returns:
        MagicMock with encode() returning a list of the given length.
    """
    enc = MagicMock()
    enc.encode.return_value = list(range(token_count))
    return enc


# ─── Tests: _scan_files ───────────────────────────────────────────────────────


def test_scan_files_finds_matching_extensions(
    logic: LLMPackagerLogic, temp_project: Path
) -> None:
    """_scan_files returns only files with matching extensions."""
    result = logic._scan_files(temp_project, [".py"], [])
    names = {f.name for f in result}
    assert "main.py" in names
    assert "helper.py" in names
    assert "utils.ts" not in names


def test_scan_files_excludes_patterns(logic: LLMPackagerLogic, temp_project: Path) -> None:
    """_scan_files excludes files matching gitignore patterns."""
    result = logic._scan_files(temp_project, [".js"], ["**/node_modules/**"])
    assert all("node_modules" not in str(f) for f in result)


def test_scan_files_returns_sorted(logic: LLMPackagerLogic, temp_project: Path) -> None:
    """_scan_files returns paths in sorted order."""
    result = logic._scan_files(temp_project, [".py", ".ts"], [])
    assert result == sorted(result)


# ─── Tests: _safe_read ────────────────────────────────────────────────────────


def test_safe_read_returns_content(logic: LLMPackagerLogic, tmp_path: Path) -> None:
    """_safe_read returns file content for a valid UTF-8 file."""
    f = tmp_path / "test.py"
    f.write_text("hello world\n", encoding="utf-8")
    assert logic._safe_read(f) == "hello world\n"


def test_safe_read_skips_large_file(logic: LLMPackagerLogic, tmp_path: Path) -> None:
    """_safe_read returns None for files exceeding MAX_FILE_SIZE_BYTES."""
    f = tmp_path / "big.bin"
    with patch.object(Path, "stat") as mock_stat:
        mock_stat.return_value = MagicMock(st_size=MAX_FILE_SIZE_BYTES + 1)
        result = logic._safe_read(f)
    assert result is None


def test_safe_read_handles_permission_error(logic: LLMPackagerLogic, tmp_path: Path) -> None:
    """_safe_read returns None when the file cannot be read."""
    f = tmp_path / "locked.py"
    f.write_text("x", encoding="utf-8")
    with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
        result = logic._safe_read(f)
    assert result is None


# ─── Tests: _count_tokens ─────────────────────────────────────────────────────


@pytest.fixture()
def vocabulary_present() -> Any:
    """Report a locally cached tiktoken vocabulary."""
    with patch(
        "modules.extractors.llm_packager.logic.vocabulary_is_available",
        return_value=True,
    ) as patched:
        yield patched


def test_count_tokens_valid_model(
    logic: LLMPackagerLogic, vocabulary_present: Any
) -> None:
    """_count_tokens returns an exact count when a vocabulary is cached."""
    with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(7)):
        count, approximate = logic._count_tokens("hello world", "cl100k_base")
    assert count == 7
    assert approximate is False


def test_count_tokens_fallback_on_bad_model(
    logic: LLMPackagerLogic, vocabulary_present: Any
) -> None:
    """_count_tokens falls back to the default encoding on unknown model names."""
    fallback_enc = _make_mock_encoder(4)

    def _side_effect(model: str) -> MagicMock:
        if model == "nonexistent_model_xyz":
            raise ValueError("unknown model")
        return fallback_enc

    with patch("tiktoken.get_encoding", side_effect=_side_effect):
        count, approximate = logic._count_tokens("hello world", "nonexistent_model_xyz")
    assert count == 4
    assert approximate is False


# ─── Tests: offline token counting (rule C-01) ────────────────────────────────


def test_no_vocabulary_estimates_instead_of_downloading(
    logic: LLMPackagerLogic,
) -> None:
    """tiktoken must never be asked to fetch its vocabulary over the network."""
    text = "x" * 400
    with patch(
        "modules.extractors.llm_packager.logic.vocabulary_is_available",
        return_value=False,
    ):
        with patch("tiktoken.get_encoding") as get_encoding:
            count, approximate = logic._count_tokens(text, "o200k_base")

    get_encoding.assert_not_called()
    assert approximate is True
    assert count == len(text) // APPROX_CHARS_PER_TOKEN


def test_estimate_used_when_every_encoding_fails(
    logic: LLMPackagerLogic, vocabulary_present: Any
) -> None:
    """A corrupt cache must degrade to an estimate, not raise."""
    with patch("tiktoken.get_encoding", side_effect=OSError("corrupt cache blob")):
        count, approximate = logic._count_tokens("x" * 200, "o200k_base")

    assert approximate is True
    assert count == 200 // APPROX_CHARS_PER_TOKEN


def test_result_flags_an_approximate_count(
    logic: LLMPackagerLogic, temp_project: Path, tmp_path: Path
) -> None:
    """The UI needs to know whether the number it shows is exact."""
    params = PackageParams(
        source_dir=temp_project, extensions=[".py"], output_dir=tmp_path / "out"
    )
    with patch(
        "modules.extractors.llm_packager.logic.vocabulary_is_available",
        return_value=False,
    ):
        asyncio.run(_drain(logic.execute(params)))

    assert logic._last_result is not None
    assert logic._last_result.token_count_is_approximate is True


async def _drain(generator: Any) -> None:
    """Exhaust an async generator, discarding its events."""
    async for _event in generator:
        pass


class TestVocabularyProbe:
    def test_env_override_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
        assert tiktoken_cache_dir() == tmp_path

    def test_falls_back_to_the_temp_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for variable in TIKTOKEN_CACHE_ENV_VARS:
            monkeypatch.delenv(variable, raising=False)
        assert tiktoken_cache_dir().name == TIKTOKEN_CACHE_DIRNAME

    def test_a_populated_cache_is_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "abc123").write_bytes(b"vocabulary blob")
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
        assert vocabulary_is_available() is True

    def test_an_empty_cache_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
        assert vocabulary_is_available() is False

    def test_a_missing_cache_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "absent"))
        assert vocabulary_is_available() is False

    def test_an_unreadable_cache_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))

        def refuse(_self: Path) -> object:
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "iterdir", refuse)
        assert vocabulary_is_available() is False


# ─── Tests: _write_output ─────────────────────────────────────────────────────


def test_write_output_creates_file(logic: LLMPackagerLogic, tmp_path: Path) -> None:
    """_write_output creates a .txt file in the output directory."""
    content = "# context\n\nprint('hello')\n"
    out = logic._write_output(content, tmp_path)
    assert out.exists()
    assert out.suffix == ".txt"
    assert out.read_text(encoding="utf-8") == content


def test_write_output_creates_dir_if_missing(
    logic: LLMPackagerLogic, tmp_path: Path
) -> None:
    """_write_output creates the output directory if it does not exist."""
    out_dir = tmp_path / "new_exports" / "sub"
    out = logic._write_output("content", out_dir)
    assert out_dir.exists()
    assert out.exists()


# ─── Tests: execute() full pipeline ──────────────────────────────────────────


@pytest.mark.asyncio()
async def test_execute_full_pipeline(
    logic: LLMPackagerLogic, temp_project: Path, tmp_path: Path
) -> None:
    """execute() yields ProgressEvents ending at 100% with an output_path."""
    params = PackageParams(
        source_dir=temp_project,
        extensions=[".py"],
        exclude_patterns=[],
        output_dir=tmp_path / "out",
    )
    with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(10)):
        events = []
        async for ev in logic.execute(params):
            events.append(ev)

    assert len(events) >= 5
    final = events[-1]
    assert final.percent == PROGRESS_WRITE_DONE
    assert final.output_path is not None
    assert final.output_path.exists()
    assert final.error is None


@pytest.mark.asyncio()
async def test_execute_skips_large_files(
    logic: LLMPackagerLogic, tmp_path: Path
) -> None:
    """execute() skips oversized files and still completes successfully."""
    (tmp_path / "small.py").write_text("x = 1\n", encoding="utf-8")

    params = PackageParams(
        source_dir=tmp_path,
        extensions=[".py"],
        output_dir=tmp_path / "out",
    )

    with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(2)):
        with patch.object(
            logic,
            "_safe_read",
            side_effect=lambda p: None if "small" in p.name else "x = 1",
        ):
            events = []
            async for ev in logic.execute(params):
                events.append(ev)

    assert events[-1].percent == PROGRESS_WRITE_DONE


# ─── Tests: run statistics (PackageResult) ────────────────────────────────────


@pytest.mark.asyncio()
async def test_execute_records_real_run_statistics(
    logic: LLMPackagerLogic, temp_project: Path, tmp_path: Path
) -> None:
    """The result must carry real counts, not placeholder zeros."""
    params = PackageParams(
        source_dir=temp_project,
        extensions=[".py"],
        exclude_patterns=[],
        output_dir=tmp_path / "out",
    )
    with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(42)):
        async for _ev in logic.execute(params):
            pass

    result = logic._last_result
    assert result is not None
    assert result.file_count == 2  # main.py and subdir/helper.py
    # Tokens are counted per file now, so the mock's 42 is charged twice.
    assert result.token_count == 84
    assert result.total_chars > 0
    assert result.skipped_count == 0
    assert result.token_model == params.token_model


@pytest.mark.asyncio()
async def test_execute_counts_skipped_files(
    logic: LLMPackagerLogic, tmp_path: Path
) -> None:
    """Unreadable or oversized files are reported in skipped_count."""
    (tmp_path / "readable.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "skipped.py").write_text("y = 2\n", encoding="utf-8")

    params = PackageParams(
        source_dir=tmp_path, extensions=[".py"], output_dir=tmp_path / "out"
    )

    with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(3)):
        with patch.object(
            logic,
            "_safe_read",
            side_effect=lambda p: None if "skipped" in p.name else "x = 1",
        ):
            async for _ev in logic.execute(params):
                pass

    result = logic._last_result
    assert result is not None
    assert result.file_count == 1
    assert result.skipped_count == 1


@pytest.mark.asyncio()
async def test_result_is_published_on_the_event_bus(
    logic: LLMPackagerLogic, temp_project: Path, tmp_path: Path
) -> None:
    """The UI learns the run statistics only through the done event."""
    from core.event_bus import event_bus
    from modules.extractors.llm_packager.constants import EVENT_DONE
    from modules.extractors.llm_packager.models import PackageResult

    received: list[PackageResult] = []

    async def capture(payload: object) -> None:
        assert isinstance(payload, PackageResult)
        received.append(payload)

    event_bus.subscribe(EVENT_DONE, capture)
    try:
        params = PackageParams(
            source_dir=temp_project,
            extensions=[".py"],
            exclude_patterns=[],
            output_dir=tmp_path / "out",
        )
        with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(11)):
            await logic._on_execute(params)
    finally:
        event_bus.unsubscribe(EVENT_DONE, capture)

    assert len(received) == 1
    assert received[0].token_count == 22  # 11 tokens per file, two files
    assert received[0].file_count == 2


@pytest.mark.asyncio()
async def test_a_failed_run_publishes_no_result(
    logic: LLMPackagerLogic, temp_project: Path, tmp_path: Path
) -> None:
    """A stale result from an earlier run must not be republished on failure."""
    from core.event_bus import event_bus
    from modules.extractors.llm_packager.constants import EVENT_DONE, EVENT_ERROR

    done: list[object] = []
    errors: list[object] = []

    async def on_done(payload: object) -> None:
        done.append(payload)

    async def on_error(payload: object) -> None:
        errors.append(payload)

    event_bus.subscribe(EVENT_DONE, on_done)
    event_bus.subscribe(EVENT_ERROR, on_error)
    try:
        params = PackageParams(
            source_dir=temp_project, extensions=[".py"], output_dir=tmp_path / "out"
        )
        with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(5)):
            await logic._on_execute(params)
        assert len(done) == 1

        with patch.object(logic, "_scan_files", side_effect=OSError("disk failure")):
            await logic._on_execute(params)
    finally:
        event_bus.unsubscribe(EVENT_DONE, on_done)
        event_bus.unsubscribe(EVENT_ERROR, on_error)

    assert len(done) == 1
    assert len(errors) == 1


@pytest.mark.asyncio()
async def test_a_non_params_payload_is_rejected(logic: LLMPackagerLogic) -> None:
    await logic._on_execute({"source_dir": "/tmp"})
    assert logic._last_result is None


# ─── Tests: EventBus registration ─────────────────────────────────────────────


@pytest.mark.asyncio()
async def test_register_subscribes_the_execute_handler(logic: LLMPackagerLogic) -> None:
    from core.event_bus import event_bus
    from modules.extractors.llm_packager.constants import EVENT_EXECUTE

    await logic.register()
    try:
        assert logic._on_execute in event_bus._subscribers[EVENT_EXECUTE]
    finally:
        await logic.unregister()


@pytest.mark.asyncio()
async def test_unregister_removes_the_execute_handler(logic: LLMPackagerLogic) -> None:
    from core.event_bus import event_bus
    from modules.extractors.llm_packager.constants import EVENT_EXECUTE

    await logic.register()
    await logic.unregister()

    assert logic._on_execute not in event_bus._subscribers[EVENT_EXECUTE]


# ─── Table of contents ────────────────────────────────────────────────────────


def _entry(path: str, lines: int = 10, tokens: int = 100) -> PackagedFile:
    """Build a packaged file for TOC and chunking tests."""
    return PackagedFile(
        relative_path=path, content="x\n" * lines, lines=lines, tokens=tokens
    )


class TestTableOfContents:
    def test_lists_every_file(self, logic: LLMPackagerLogic) -> None:
        toc = logic.build_toc([_entry("a.py"), _entry("sub/b.py")], approximate=False)
        assert "a.py" in toc
        assert "sub/b.py" in toc

    def test_shows_line_and_token_counts(self, logic: LLMPackagerLogic) -> None:
        toc = logic.build_toc([_entry("a.py", lines=42, tokens=333)], approximate=False)
        assert "42 lines" in toc
        assert "333 tokens" in toc

    def test_numbers_the_entries(self, logic: LLMPackagerLogic) -> None:
        toc = logic.build_toc([_entry("a.py"), _entry("b.py")], approximate=False)
        assert "1. a.py" in toc
        assert "2. b.py" in toc

    def test_reports_totals(self, logic: LLMPackagerLogic) -> None:
        toc = logic.build_toc(
            [_entry("a.py", lines=10, tokens=100), _entry("b.py", lines=5, tokens=50)],
            approximate=False,
        )
        assert "2 files" in toc
        assert "15 lines" in toc
        assert "150 tokens" in toc

    def test_marks_estimated_counts(self, logic: LLMPackagerLogic) -> None:
        toc = logic.build_toc([_entry("a.py", tokens=100)], approximate=True)
        assert "≈100 tokens" in toc

    def test_is_prepended_to_the_output(
        self, logic: LLMPackagerLogic, temp_project: Path, tmp_path: Path
    ) -> None:
        params = PackageParams(
            source_dir=temp_project,
            extensions=[".py"],
            output_dir=tmp_path / "out",
            include_toc=True,
        )
        with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(5)):
            asyncio.run(_drain(logic.execute(params)))

        content = logic._last_result.output_paths[0].read_text(encoding="utf-8")  # type: ignore[union-attr]
        assert content.startswith("=")
        assert "TABLE OF CONTENTS" in content
        assert content.index("TABLE OF CONTENTS") < content.index("# FILE:")

    def test_can_be_disabled(
        self, logic: LLMPackagerLogic, temp_project: Path, tmp_path: Path
    ) -> None:
        params = PackageParams(
            source_dir=temp_project,
            extensions=[".py"],
            output_dir=tmp_path / "out",
            include_toc=False,
        )
        with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(5)):
            asyncio.run(_drain(logic.execute(params)))

        content = logic._last_result.output_paths[0].read_text(encoding="utf-8")  # type: ignore[union-attr]
        assert "TABLE OF CONTENTS" not in content


# ─── Chunking ─────────────────────────────────────────────────────────────────


class TestChunking:
    def test_disabled_by_default(self, logic: LLMPackagerLogic) -> None:
        entries = [_entry(f"f{i}.py", tokens=500) for i in range(10)]
        groups, warnings = logic._chunk(entries, limit=0)

        assert len(groups) == 1
        assert warnings == []

    def test_splits_once_the_limit_is_exceeded(self, logic: LLMPackagerLogic) -> None:
        entries = [_entry(f"f{i}.py", tokens=400) for i in range(5)]
        groups, _warnings = logic._chunk(entries, limit=1000)

        assert len(groups) == 3
        assert [len(g) for g in groups] == [2, 2, 1]

    def test_no_chunk_exceeds_the_limit(self, logic: LLMPackagerLogic) -> None:
        entries = [_entry(f"f{i}.py", tokens=300) for i in range(9)]
        groups, _warnings = logic._chunk(entries, limit=1000)

        assert all(sum(e.tokens for e in g) <= 1000 for g in groups)

    def test_files_are_never_split_across_chunks(
        self, logic: LLMPackagerLogic
    ) -> None:
        """Half a source file is worse than useless to a model."""
        entries = [_entry(f"f{i}.py", tokens=400) for i in range(5)]
        groups, _warnings = logic._chunk(entries, limit=1000)

        flattened = [e.relative_path for g in groups for e in g]
        assert flattened == [e.relative_path for e in entries]
        assert len(flattened) == len(set(flattened))

    def test_an_oversized_file_gets_its_own_chunk(
        self, logic: LLMPackagerLogic
    ) -> None:
        entries = [_entry("small.py", tokens=100), _entry("huge.py", tokens=5000)]
        groups, warnings = logic._chunk(entries, limit=1000)

        assert [e.relative_path for e in groups[-1]] == ["huge.py"]
        assert any("huge.py" in note for note in warnings)

    def test_an_oversized_file_flushes_the_pending_chunk(
        self, logic: LLMPackagerLogic
    ) -> None:
        entries = [
            _entry("a.py", tokens=100),
            _entry("huge.py", tokens=5000),
            _entry("b.py", tokens=100),
        ]
        groups, _warnings = logic._chunk(entries, limit=1000)

        assert [[e.relative_path for e in g] for g in groups] == [
            ["a.py"],
            ["huge.py"],
            ["b.py"],
        ]

    def test_an_empty_selection_is_handled(self, logic: LLMPackagerLogic) -> None:
        groups, warnings = logic._chunk([], limit=1000)
        assert groups == [[]]
        assert warnings == []

    def test_chunked_output_writes_numbered_files(
        self, logic: LLMPackagerLogic, tmp_path: Path
    ) -> None:
        source = tmp_path / "project"
        source.mkdir()
        for index in range(6):
            (source / f"file{index}.py").write_text("x = 1\n" * 50, encoding="utf-8")

        params = PackageParams(
            source_dir=source,
            extensions=[".py"],
            output_dir=tmp_path / "out",
            max_tokens_per_chunk=120,
        )
        with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(100)):
            asyncio.run(_drain(logic.execute(params)))

        result = logic._last_result
        assert result is not None
        assert len(result.output_paths) == 6
        assert "part01of06" in result.output_paths[0].name
        assert all(p.is_file() for p in result.output_paths)

    def test_each_chunk_carries_its_own_toc(
        self, logic: LLMPackagerLogic, tmp_path: Path
    ) -> None:
        source = tmp_path / "project"
        source.mkdir()
        for index in range(4):
            (source / f"file{index}.py").write_text("x = 1\n", encoding="utf-8")

        params = PackageParams(
            source_dir=source,
            extensions=[".py"],
            output_dir=tmp_path / "out",
            max_tokens_per_chunk=120,
            include_toc=True,
        )
        with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(100)):
            asyncio.run(_drain(logic.execute(params)))

        for path in logic._last_result.output_paths:  # type: ignore[union-attr]
            assert "TABLE OF CONTENTS" in path.read_text(encoding="utf-8")

    def test_a_single_chunk_keeps_the_plain_filename(
        self, logic: LLMPackagerLogic, temp_project: Path, tmp_path: Path
    ) -> None:
        params = PackageParams(
            source_dir=temp_project,
            extensions=[".py"],
            output_dir=tmp_path / "out",
            max_tokens_per_chunk=100_000,
        )
        with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(5)):
            asyncio.run(_drain(logic.execute(params)))

        assert "part" not in logic._last_result.output_paths[0].name  # type: ignore[union-attr]

    def test_chunk_count_is_reported(
        self, logic: LLMPackagerLogic, temp_project: Path, tmp_path: Path
    ) -> None:
        params = PackageParams(
            source_dir=temp_project, extensions=[".py"], output_dir=tmp_path / "out"
        )
        with patch("tiktoken.get_encoding", return_value=_make_mock_encoder(5)):
            asyncio.run(_drain(logic.execute(params)))

        assert logic._last_result.chunk_count == 1  # type: ignore[union-attr]


def test_scan_skips_paths_outside_the_source_root(
    logic: LLMPackagerLogic, tmp_path: Path
) -> None:
    """A path that cannot be made relative to the root is skipped, not fatal."""
    (tmp_path / "inside.py").write_text("x = 1\n", encoding="utf-8")
    outsider = tmp_path.parent / "outside.py"

    real_relative_to = Path.relative_to

    def fake_relative_to(self: Path, *args: object, **kwargs: object) -> Path:
        if self.name == "inside.py":
            raise ValueError("not relative")
        return real_relative_to(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "relative_to", fake_relative_to):
        result = logic._scan_files(tmp_path, [".py"], [])

    assert outsider not in result
    assert result == []
