"""Unit tests for the media_suite logic layer.

FFmpeg is not invoked. Every test intercepts the subprocess boundary and
asserts on the command that *would* run, so the argument construction — which
is where the real risk lies — is verified without a 2 GB encode.
"""
from __future__ import annotations

import asyncio
import io
import json
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from core.models import ProgressEvent
from modules.converters.media_suite.constants import (
    AUDIO_BITRATE_KBPS,
    LOUDNESS_TARGET_LUFS,
    MIN_VIDEO_BITRATE_KBPS,
    OUTPUT_SUBDIR,
    PRESET_TARGET_MB,
    PROGRESS_COMPLETE,
)
from modules.converters.media_suite.logic import (
    FFmpegError,
    MediaSuiteLogic,
    ffmpeg_available,
    find_binary,
)
from modules.converters.media_suite.models import (
    MediaInfo,
    MediaOperation,
    MediaParams,
    MediaResult,
    SizePreset,
)


@pytest.fixture()
def logic() -> MediaSuiteLogic:
    """Fresh logic instance with no EventBus registrations."""
    return MediaSuiteLogic()


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    """Directory operations write into."""
    return tmp_path / "exports"


@pytest.fixture()
def video(tmp_path: Path) -> Path:
    """A stand-in video file; FFmpeg never actually reads it."""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * 4096)
    return path


class _FakePopen:
    """Stand-in for the FFmpeg process, including its -progress stream.

    ``run_ffmpeg`` reads progress from stdout and errors from stderr, so the
    fake has to behave like a context-managed Popen with both pipes.
    """

    def __init__(
        self,
        command: list[str],
        returncode: int = 0,
        stderr_text: str = "",
        progress_lines: list[str] | None = None,
        on_start: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.args = command
        self.returncode = returncode
        self.stdout = iter(progress_lines or [])
        self.stderr = io.StringIO(stderr_text)
        self.killed = False
        if on_start is not None:
            on_start(command)

    def __enter__(self) -> _FakePopen:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.fixture()
def available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report FFmpeg and ffprobe as present."""
    monkeypatch.setattr(
        "modules.converters.media_suite.logic.find_binary",
        lambda name: Path(f"/usr/bin/{name}"),
    )


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Intercept every FFmpeg invocation and record its command."""
    commands: list[list[str]] = []

    def fake_popen(command: list[str], **_kwargs: object) -> _FakePopen:
        commands.append(command)
        # The last argument is the output path for every command we build.
        target = Path(command[-1])
        if "%03d" not in target.name:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"encoded output")
        # A realistic slice of FFmpeg's -progress stream.
        return _FakePopen(
            command,
            progress_lines=[
                "frame=1\n",
                "out_time_us=500000\n",
                "progress=end\n",
            ],
        )

    monkeypatch.setattr(
        "modules.converters.media_suite.logic.subprocess.Popen", fake_popen
    )
    return commands


@pytest.fixture()
def probed(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, object]]:
    """Control what ``probe`` reports, without running ffprobe."""
    settings: dict[str, object] = {
        "duration_seconds": 60.0,
        "width": 1920,
        "height": 1080,
        "has_audio": True,
    }
    monkeypatch.setattr(
        MediaSuiteLogic, "probe", lambda _self, _source: MediaInfo(**settings)  # type: ignore[arg-type]
    )
    yield settings


def run(
    logic: MediaSuiteLogic, params: MediaParams
) -> tuple[list[ProgressEvent], MediaResult]:
    """Execute an operation to completion."""

    async def drive() -> list[ProgressEvent]:
        return [event async for event in logic.execute(params)]

    events = asyncio.run(drive())
    assert logic._last_result is not None
    return events, logic._last_result


def _params(
    operation: MediaOperation, inputs: list[Path], out: Path, **kw: object
) -> MediaParams:
    """Build MediaParams with the common fields filled in."""
    return MediaParams(
        operation=operation, input_paths=inputs, output_dir=out, **kw
    )  # type: ignore[arg-type]


def flatten(command: list[str]) -> str:
    """Join a command for substring assertions."""
    return " ".join(command)


# ─── Binary discovery ─────────────────────────────────────────────────────────


class TestBinaryDiscovery:
    def test_a_bundled_binary_wins_over_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A packaged build must use its own FFmpeg, not whatever is installed."""
        monkeypatch.chdir(tmp_path)
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.is_windows", lambda: False
        )
        (bundled / "ffmpeg").write_bytes(b"binary")
        monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/ffmpeg")

        assert find_binary("ffmpeg") == (bundled / "ffmpeg").resolve()

    def test_windows_looks_for_an_exe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.is_windows", lambda: True
        )
        (bundled / "ffmpeg.exe").write_bytes(b"binary")
        monkeypatch.setattr("shutil.which", lambda _n: None)

        assert find_binary("ffmpeg") == (bundled / "ffmpeg.exe").resolve()

    def test_falls_back_to_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/ffmpeg")
        assert find_binary("ffmpeg") == Path("/usr/bin/ffmpeg")

    def test_returns_none_when_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("shutil.which", lambda _n: None)
        assert find_binary("ffmpeg") is None

    def test_availability_needs_both_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ffprobe is what supplies duration; ffmpeg alone is not enough."""
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.find_binary",
            lambda name: Path("/usr/bin/ffmpeg") if name == "ffmpeg" else None,
        )
        assert ffmpeg_available() is False


# ─── Bitrate arithmetic ───────────────────────────────────────────────────────


class TestBitrateCalculation:
    def test_derives_a_bitrate_from_size_and_duration(
        self, logic: MediaSuiteLogic
    ) -> None:
        bitrate = logic.video_bitrate_kbps(10, 60.0)
        assert bitrate is not None
        assert MIN_VIDEO_BITRATE_KBPS < bitrate < 1500

    def test_a_longer_clip_gets_a_lower_bitrate(self, logic: MediaSuiteLogic) -> None:
        short = logic.video_bitrate_kbps(10, 30.0)
        long = logic.video_bitrate_kbps(10, 300.0)
        assert short is not None and long is not None
        assert short > long

    def test_a_larger_target_gets_a_higher_bitrate(
        self, logic: MediaSuiteLogic
    ) -> None:
        small = logic.video_bitrate_kbps(10, 60.0)
        large = logic.video_bitrate_kbps(50, 60.0)
        assert small is not None and large is not None
        assert large > small

    def test_the_audio_track_is_subtracted_from_the_budget(
        self, logic: MediaSuiteLogic
    ) -> None:
        """Ignoring the audio budget is what makes encodes overshoot."""
        bitrate = logic.video_bitrate_kbps(10, 60.0)
        naive = int((10 * 1024 * 1024 * 8 / 1000) / 60.0)
        assert bitrate is not None
        assert bitrate < naive - AUDIO_BITRATE_KBPS + 1

    def test_an_impossible_target_floors_at_the_minimum(
        self, logic: MediaSuiteLogic
    ) -> None:
        assert logic.video_bitrate_kbps(1, 7200.0) == MIN_VIDEO_BITRATE_KBPS

    @pytest.mark.parametrize("duration", [0.0, -5.0])
    def test_an_unknown_duration_yields_no_bitrate(
        self, logic: MediaSuiteLogic, duration: float
    ) -> None:
        assert logic.video_bitrate_kbps(10, duration) is None

    @pytest.mark.parametrize("preset", [SizePreset.DISCORD, SizePreset.EMAIL, SizePreset.WEB])
    def test_presets_resolve_to_their_ceiling(
        self, logic: MediaSuiteLogic, tmp_path: Path, preset: SizePreset
    ) -> None:
        params = _params(
            MediaOperation.COMPRESS_VIDEO,
            [tmp_path / "a.mp4"],
            tmp_path,
            preset=preset,
        )
        assert logic.target_megabytes(params) == PRESET_TARGET_MB[preset.value]

    def test_custom_uses_the_supplied_size(
        self, logic: MediaSuiteLogic, tmp_path: Path
    ) -> None:
        params = _params(
            MediaOperation.COMPRESS_VIDEO,
            [tmp_path / "a.mp4"],
            tmp_path,
            preset=SizePreset.CUSTOM,
            target_mb=17,
        )
        assert logic.target_megabytes(params) == 17


# ─── Command construction ─────────────────────────────────────────────────────


class TestCompressVideo:
    def test_targets_the_calculated_bitrate(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        run(logic, _params(MediaOperation.COMPRESS_VIDEO, [video], out_dir))
        command = flatten(captured[0])

        assert "-b:v" in command
        assert "libx264" in command

    def test_audio_is_encoded_when_present(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        run(logic, _params(MediaOperation.COMPRESS_VIDEO, [video], out_dir))
        assert "-c:a aac" in flatten(captured[0])

    def test_a_silent_clip_disables_audio(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        """Asking for an audio codec on a silent input makes FFmpeg fail."""
        probed["has_audio"] = False
        run(logic, _params(MediaOperation.COMPRESS_VIDEO, [video], out_dir))
        assert "-an" in flatten(captured[0])

    def test_an_unknown_duration_falls_back_to_constant_quality(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        probed["duration_seconds"] = 0.0
        _events, result = run(
            logic, _params(MediaOperation.COMPRESS_VIDEO, [video], out_dir)
        )

        assert "-crf" in flatten(captured[0])
        assert any("duration unknown" in note for note in result.warnings)

    def test_an_impossible_target_warns_about_the_minimum_bitrate(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        """A 1 MB ceiling on a two-hour clip cannot be honoured; say so."""
        probed["duration_seconds"] = 7200.0
        _events, result = run(
            logic,
            _params(
                MediaOperation.COMPRESS_VIDEO,
                [video],
                out_dir,
                preset=SizePreset.CUSTOM,
                target_mb=1,
            ),
        )
        assert any("minimum bitrate" in note for note in result.warnings)

    def test_an_oversized_result_is_reported(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        probed: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Silently handing back a 40 MB file for a 10 MB target is a trap."""

        def oversized(command: list[str], **_kw: object) -> _FakePopen:
            target = Path(command[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * (11 * 1024 * 1024))
            return _FakePopen(command)

        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen", oversized
        )

        _events, result = run(
            logic,
            _params(
                MediaOperation.COMPRESS_VIDEO, [video], out_dir, preset=SizePreset.DISCORD
            ),
        )
        assert any("above 10 MB" in note for note in result.warnings)


class TestExtractAudio:
    def test_builds_an_mp3_command(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        _events, result = run(
            logic, _params(MediaOperation.EXTRACT_AUDIO, [video], out_dir)
        )
        command = flatten(captured[0])

        assert "-vn" in command
        assert "libmp3lame" in command
        assert result.output_paths[0].suffix == ".mp3"

    def test_a_silent_video_is_reported_not_encoded(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        probed["has_audio"] = False
        _events, result = run(
            logic, _params(MediaOperation.EXTRACT_AUDIO, [video], out_dir)
        )

        assert captured == []
        assert result.output_paths == []
        assert any("no audio track" in note for note in result.warnings)


class TestToMp4:
    def test_enables_faststart_for_streaming(
        self,
        logic: MediaSuiteLogic,
        tmp_path: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        source = tmp_path / "clip.webm"
        source.write_bytes(b"\x00" * 100)

        _events, result = run(logic, _params(MediaOperation.TO_MP4, [source], out_dir))

        assert "+faststart" in flatten(captured[0])
        assert result.output_paths[0].suffix == ".mp4"


class TestThumbnails:
    def test_spreads_frames_across_the_clip(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        run(
            logic,
            _params(MediaOperation.THUMBNAILS, [video], out_dir, thumbnail_count=6),
        )
        command = flatten(captured[0])

        # 60s / 6 frames = one frame every 10 seconds.
        assert "fps=1/10" in command
        assert "-frames:v 6" in command

    def test_produces_a_folder(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        _events, result = run(
            logic, _params(MediaOperation.THUMBNAILS, [video], out_dir)
        )
        assert result.output_paths[0].is_dir()

    def test_an_unknown_duration_still_grabs_frames(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        probed["duration_seconds"] = 0.0
        _events, result = run(
            logic, _params(MediaOperation.THUMBNAILS, [video], out_dir)
        )

        assert "fps=" not in flatten(captured[0])
        assert any("duration unknown" in note for note in result.warnings)


class TestNormalizeAudio:
    def test_applies_the_ebu_r128_target(
        self,
        logic: MediaSuiteLogic,
        tmp_path: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        source = tmp_path / "track.mp3"
        source.write_bytes(b"\x00" * 100)

        run(logic, _params(MediaOperation.NORMALIZE_AUDIO, [source], out_dir))

        assert f"loudnorm=I={LOUDNESS_TARGET_LUFS}" in flatten(captured[0])

    def test_the_source_extension_is_preserved(
        self,
        logic: MediaSuiteLogic,
        tmp_path: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        source = tmp_path / "track.flac"
        source.write_bytes(b"\x00" * 100)

        _events, result = run(
            logic, _params(MediaOperation.NORMALIZE_AUDIO, [source], out_dir)
        )
        assert result.output_paths[0].suffix == ".flac"


# ─── FFmpeg invocation ────────────────────────────────────────────────────────


class TestRunFfmpeg:
    def test_a_failure_surfaces_the_last_error_line(
        self, logic: MediaSuiteLogic, available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen",
            lambda command, **_k: _FakePopen(
                command,
                returncode=1,
                stderr_text="noise\nInvalid data found when processing input",
            ),
        )
        with pytest.raises(FFmpegError, match="Invalid data found"):
            logic.run_ffmpeg(["-i", "x", "y"])

    def test_a_failure_without_output_still_reports(
        self, logic: MediaSuiteLogic, available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen",
            lambda command, **_k: _FakePopen(command, returncode=1),
        )
        with pytest.raises(FFmpegError, match="no error output"):
            logic.run_ffmpeg(["-i", "x", "y"])

    def test_a_hung_encode_times_out(
        self, logic: MediaSuiteLogic, available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rule B-02 — a wedged process must not hang the application."""

        def hang(*_a: object, **_k: object) -> None:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=3600)

        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen", hang
        )
        with pytest.raises(FFmpegError, match="exceeded"):
            logic.run_ffmpeg(["-i", "x", "y"])

    def test_an_unstartable_binary_is_reported(
        self, logic: MediaSuiteLogic, available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_a: object, **_k: object) -> None:
            raise OSError("exec format error")

        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.run", refuse
        )
        with pytest.raises(FFmpegError, match="could not be started"):
            logic.run_ffmpeg(["-i", "x", "y"])

    def test_a_missing_binary_is_reported(
        self, logic: MediaSuiteLogic, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.find_binary", lambda _n: None
        )
        with pytest.raises(FFmpegError, match="not available"):
            logic.run_ffmpeg(["-i", "x", "y"])

    def test_output_is_overwritten_without_prompting(
        self, logic: MediaSuiteLogic, available: None, captured: list[list[str]],
        tmp_path: Path,
    ) -> None:
        """Without -y FFmpeg blocks on an interactive prompt forever.

        The output path goes under ``tmp_path``: the ``captured`` fixture
        actually writes the file it is handed, so a bare relative name left an
        ``out.mp4`` in whatever directory the suite was run from — the repo
        root, in practice.
        """
        logic.run_ffmpeg(["-i", "x", str(tmp_path / "out.mp4")])
        assert "-y" in captured[0]


# ─── Probing ──────────────────────────────────────────────────────────────────


class TestProbe:
    def _probe_returning(
        self, monkeypatch: pytest.MonkeyPatch, payload: object, returncode: int = 0
    ) -> None:
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.find_binary",
            lambda _n: Path("/usr/bin/ffprobe"),
        )
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.run",
            lambda *_a, **_k: subprocess.CompletedProcess(
                [], returncode, stdout=json.dumps(payload), stderr=""
            ),
        )

    def test_reads_duration_and_dimensions(
        self, logic: MediaSuiteLogic, monkeypatch: pytest.MonkeyPatch, video: Path
    ) -> None:
        self._probe_returning(
            monkeypatch,
            {
                "format": {"duration": "125.5"},
                "streams": [
                    {"codec_type": "video", "width": 1280, "height": 720},
                    {"codec_type": "audio"},
                ],
            },
        )
        info = logic.probe(video)

        assert info.duration_seconds == pytest.approx(125.5)
        assert (info.width, info.height) == (1280, 720)
        assert info.has_audio is True

    def test_detects_a_silent_video(
        self, logic: MediaSuiteLogic, monkeypatch: pytest.MonkeyPatch, video: Path
    ) -> None:
        self._probe_returning(
            monkeypatch,
            {"format": {"duration": "10"}, "streams": [{"codec_type": "video"}]},
        )
        assert logic.probe(video).has_audio is False

    def test_a_missing_duration_reports_zero(
        self, logic: MediaSuiteLogic, monkeypatch: pytest.MonkeyPatch, video: Path
    ) -> None:
        self._probe_returning(monkeypatch, {"streams": []})
        assert logic.probe(video).duration_seconds == 0.0

    def test_an_unparsable_duration_reports_zero(
        self, logic: MediaSuiteLogic, monkeypatch: pytest.MonkeyPatch, video: Path
    ) -> None:
        self._probe_returning(monkeypatch, {"format": {"duration": "N/A"}, "streams": []})
        assert logic.probe(video).duration_seconds == 0.0

    def test_a_failed_probe_degrades_quietly(
        self, logic: MediaSuiteLogic, monkeypatch: pytest.MonkeyPatch, video: Path
    ) -> None:
        self._probe_returning(monkeypatch, {}, returncode=1)
        assert logic.probe(video) == MediaInfo()

    def test_malformed_json_degrades_quietly(
        self, logic: MediaSuiteLogic, monkeypatch: pytest.MonkeyPatch, video: Path
    ) -> None:
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.find_binary",
            lambda _n: Path("/usr/bin/ffprobe"),
        )
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.run",
            lambda *_a, **_k: subprocess.CompletedProcess(
                [], 0, stdout="{not json", stderr=""
            ),
        )
        assert logic.probe(video) == MediaInfo()

    def test_a_missing_ffprobe_degrades_quietly(
        self, logic: MediaSuiteLogic, monkeypatch: pytest.MonkeyPatch, video: Path
    ) -> None:
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.find_binary", lambda _n: None
        )
        assert logic.probe(video) == MediaInfo()

    def test_a_probe_timeout_degrades_quietly(
        self, logic: MediaSuiteLogic, monkeypatch: pytest.MonkeyPatch, video: Path
    ) -> None:
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.find_binary",
            lambda _n: Path("/usr/bin/ffprobe"),
        )

        def hang(*_a: object, **_k: object) -> None:
            raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=1)

        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.run", hang
        )
        assert logic.probe(video) == MediaInfo()


# ─── Shared behaviour ─────────────────────────────────────────────────────────


class TestExecutionContract:
    def test_every_operation_has_a_handler(self, logic: MediaSuiteLogic) -> None:
        assert set(logic._handlers()) == set(MediaOperation)

    def test_a_missing_ffmpeg_stops_before_touching_files(
        self, logic: MediaSuiteLogic, video: Path, out_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.ffmpeg_available", lambda: False
        )
        with pytest.raises(FFmpegError, match="not available"):
            run(logic, _params(MediaOperation.TO_MP4, [video], out_dir))

    def test_a_missing_input_is_reported_clearly(
        self, logic: MediaSuiteLogic, tmp_path: Path, out_dir: Path, available: None
    ) -> None:
        with pytest.raises(FileNotFoundError, match="File not found"):
            run(
                logic,
                _params(MediaOperation.TO_MP4, [tmp_path / "absent.mp4"], out_dir),
            )

    def test_a_mismatched_extension_is_rejected(
        self, tmp_path: Path, out_dir: Path
    ) -> None:
        source = tmp_path / "notes.txt"
        source.write_bytes(b"x")
        with pytest.raises(ValueError, match="can read"):
            _params(MediaOperation.TO_MP4, [source], out_dir)

    def test_audio_files_are_accepted_for_normalising(
        self, tmp_path: Path, out_dir: Path
    ) -> None:
        source = tmp_path / "track.wav"
        source.write_bytes(b"x")
        params = _params(MediaOperation.NORMALIZE_AUDIO, [source], out_dir)
        assert params.is_audio_only_input is True

    def test_writes_into_the_module_subdirectory(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        _events, result = run(logic, _params(MediaOperation.TO_MP4, [video], out_dir))
        assert result.output_paths[0].parent.name == OUTPUT_SUBDIR

    def test_progress_ends_at_one_hundred(
        self,
        logic: MediaSuiteLogic,
        video: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        events, _result = run(logic, _params(MediaOperation.TO_MP4, [video], out_dir))
        assert events[-1].percent == PROGRESS_COMPLETE

    def test_several_files_are_processed(
        self,
        logic: MediaSuiteLogic,
        tmp_path: Path,
        out_dir: Path,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
    ) -> None:
        sources = []
        for index in range(3):
            path = tmp_path / f"clip{index}.mp4"
            path.write_bytes(b"\x00" * 100)
            sources.append(path)

        _events, result = run(logic, _params(MediaOperation.TO_MP4, sources, out_dir))

        assert result.files_processed == 3
        assert len(captured) == 3

    def test_an_absent_output_measures_zero(
        self, logic: MediaSuiteLogic, tmp_path: Path
    ) -> None:
        assert logic._path_size(tmp_path / "nothing") == 0


class TestResultArithmetic:
    def _result(self, **kw: object) -> MediaResult:
        defaults: dict[str, object] = {
            "operation": MediaOperation.COMPRESS_VIDEO,
            "output_paths": [Path("out.mp4")],
            "files_processed": 1,
            "input_bytes": 1000,
            "output_bytes": 200,
        }
        return MediaResult(**{**defaults, **kw})  # type: ignore[arg-type]

    def test_reports_bytes_saved(self) -> None:
        assert self._result().bytes_saved == 800

    def test_reports_the_percentage_saved(self) -> None:
        assert self._result().size_change_percent == pytest.approx(80.0)

    def test_growth_is_negative(self) -> None:
        assert self._result(output_bytes=1600).bytes_saved == -600

    def test_an_empty_input_does_not_divide_by_zero(self) -> None:
        assert self._result(input_bytes=0).size_change_percent == 0.0


# ─── EventBus wiring ──────────────────────────────────────────────────────────


@pytest.mark.asyncio()
async def test_register_and_unregister_round_trip(logic: MediaSuiteLogic) -> None:
    from core.event_bus import event_bus
    from modules.converters.media_suite.constants import EVENT_EXECUTE

    await logic.register()
    assert logic._on_execute in event_bus._subscribers[EVENT_EXECUTE]

    await logic.unregister()
    assert logic._on_execute not in event_bus._subscribers[EVENT_EXECUTE]


@pytest.mark.asyncio()
async def test_result_is_published_on_completion(
    logic: MediaSuiteLogic,
    video: Path,
    out_dir: Path,
    available: None,
    captured: list[list[str]],
    probed: dict[str, object],
) -> None:
    from core.event_bus import event_bus
    from modules.converters.media_suite.constants import EVENT_DONE

    received: list[MediaResult] = []

    async def capture(payload: object) -> None:
        assert isinstance(payload, MediaResult)
        received.append(payload)

    event_bus.subscribe(EVENT_DONE, capture)
    try:
        await logic._on_execute(_params(MediaOperation.TO_MP4, [video], out_dir))
    finally:
        event_bus.unsubscribe(EVENT_DONE, capture)

    assert len(received) == 1


@pytest.mark.asyncio()
async def test_a_failure_publishes_an_error_not_a_result(
    logic: MediaSuiteLogic, video: Path, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.event_bus import event_bus
    from modules.converters.media_suite.constants import EVENT_DONE, EVENT_ERROR

    monkeypatch.setattr(
        "modules.converters.media_suite.logic.ffmpeg_available", lambda: False
    )
    done: list[object] = []
    errors: list[object] = []

    async def on_done(payload: object) -> None:
        done.append(payload)

    async def on_error(payload: object) -> None:
        errors.append(payload)

    event_bus.subscribe(EVENT_DONE, on_done)
    event_bus.subscribe(EVENT_ERROR, on_error)
    try:
        await logic._on_execute(_params(MediaOperation.TO_MP4, [video], out_dir))
    finally:
        event_bus.unsubscribe(EVENT_DONE, on_done)
        event_bus.unsubscribe(EVENT_ERROR, on_error)

    assert done == []
    assert len(errors) == 1


@pytest.mark.asyncio()
async def test_a_non_params_payload_is_ignored(logic: MediaSuiteLogic) -> None:
    await logic._on_execute({"operation": "to_mp4"})
    assert logic._last_result is None


# ─── §3.10e — real encode progress ───────────────────────────────────────────


class TestEncodeProgress:
    """A long encode must show movement, not one event per file.

    Previously ``execute()`` emitted a single ProgressEvent per input, so a
    ten-minute conversion showed a frozen bar for ten minutes (rule D-08).
    """

    def test_ffmpeg_is_asked_for_a_progress_stream(
        self,
        logic: MediaSuiteLogic,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
        video: Path,
        out_dir: Path,
    ) -> None:
        run(logic, _params(MediaOperation.TO_MP4, [video], out_dir))
        assert "-progress" in captured[0]
        assert "pipe:1" in captured[0]

    def test_out_time_lines_become_percentages(
        self, logic: MediaSuiteLogic, available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reported: list[int] = []
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen",
            lambda command, **_k: _FakePopen(
                command,
                progress_lines=[
                    "out_time_us=0\n",
                    "out_time_us=5000000\n",   # 5s of a 10s clip
                    "out_time_us=10000000\n",  # the whole clip
                ],
            ),
        )

        logic.run_ffmpeg(["-i", "x", "y"], duration_seconds=10.0, report=reported.append)

        assert reported == [0, 50, 99], reported

    def test_progress_is_skipped_when_the_duration_is_unknown(
        self, logic: MediaSuiteLogic, available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a duration a percentage would be a fabrication."""
        reported: list[int] = []
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen",
            lambda command, **_k: _FakePopen(
                command, progress_lines=["out_time_us=5000000\n"]
            ),
        )

        logic.run_ffmpeg(["-i", "x", "y"], duration_seconds=0.0, report=reported.append)

        assert reported == []

    def test_a_malformed_progress_line_is_ignored(
        self, logic: MediaSuiteLogic, available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FFmpeg writes out_time_us=N/A before the first frame lands."""
        reported: list[int] = []
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen",
            lambda command, **_k: _FakePopen(
                command,
                progress_lines=["out_time_us=N/A\n", "frame=3\n", "out_time_us=2500000\n"],
            ),
        )

        logic.run_ffmpeg(["-i", "x", "y"], duration_seconds=10.0, report=reported.append)

        assert reported == [25]

    def test_execute_emits_more_than_one_event_per_file(
        self,
        logic: MediaSuiteLogic,
        available: None,
        captured: list[list[str]],
        probed: dict[str, object],
        video: Path,
        out_dir: Path,
    ) -> None:
        events, _result = run(logic, _params(MediaOperation.TO_MP4, [video], out_dir))
        assert len(events) > 2
        assert events[-1].percent == PROGRESS_COMPLETE


class TestPartialOutputCleanup:
    """A failed encode must not leave a truncated file that looks finished."""

    def test_a_failed_encode_removes_its_output(
        self, logic: MediaSuiteLogic, available: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        partial = tmp_path / "half.mp4"
        partial.write_bytes(b"truncated")
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen",
            lambda command, **_k: _FakePopen(command, returncode=1, stderr_text="boom"),
        )

        with pytest.raises(FFmpegError):
            logic.run_ffmpeg(["-i", "x", str(partial)])

        assert not partial.exists()

    def test_a_timeout_removes_its_output(
        self, logic: MediaSuiteLogic, available: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        partial = tmp_path / "half.mp4"
        partial.write_bytes(b"truncated")

        def hang(*_a: object, **_k: object) -> None:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=3600)

        monkeypatch.setattr("modules.converters.media_suite.logic.subprocess.Popen", hang)

        with pytest.raises(FFmpegError, match="exceeded"):
            logic.run_ffmpeg(["-i", "x", str(partial)])

        assert not partial.exists()

    def test_a_numbered_thumbnail_pattern_is_left_alone(
        self, logic: MediaSuiteLogic, available: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """That target is a pattern, not a single file to delete."""
        real = tmp_path / "thumb_001.jpg"
        real.write_bytes(b"a thumbnail that did get written")
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen",
            lambda command, **_k: _FakePopen(command, returncode=1, stderr_text="boom"),
        )

        with pytest.raises(FFmpegError):
            logic.run_ffmpeg(["-i", "x", str(tmp_path / "thumb_%03d.jpg")])

        assert real.exists()

    def test_a_successful_encode_keeps_its_output(
        self, logic: MediaSuiteLogic, available: None, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        good = tmp_path / "done.mp4"
        good.write_bytes(b"finished")
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen",
            lambda command, **_k: _FakePopen(command),
        )

        logic.run_ffmpeg(["-i", "x", str(good)])

        assert good.exists()


# ─── Pipe handling (P0) ───────────────────────────────────────────────────────


#: How long the fake waits for its stderr to be drained before declaring the
#: deadlock. Generous enough not to flake on a loaded CI box, short enough that
#: a regression fails the suite rather than appearing to hang it.
PIPE_DEADLOCK_TIMEOUT_SECONDS: float = 10.0


class _BlockingPipePopen:
    """Models the OS pipe backpressure a real FFmpeg process applies.

    A real child blocks once its stderr pipe buffer (~64 KB) is full and stops
    producing stdout until someone drains it. ``io.StringIO`` never blocks, so
    it cannot express that; this fake makes stdout refuse to advance past its
    first line until stderr has actually been read. Draining stderr
    concurrently completes the run; draining it only after the stdout loop
    deadlocks, which is the defect under test.
    """

    def __init__(self, command: list[str]) -> None:
        self.args = command
        self.returncode = 0
        self.killed = False
        self._stderr_read = threading.Event()
        self.stdout = self._stdout_lines()
        self.stderr = self._Stderr(self._stderr_read)

    class _Stderr:
        """A stderr pipe that records the moment it is drained."""

        def __init__(self, drained: threading.Event) -> None:
            self._drained = drained

        def read(self) -> str:
            self._drained.set()
            return "warning: ignoring unsupported frame"

    def _stdout_lines(self) -> Iterator[str]:
        """Yield progress, stalling mid-stream until stderr is drained."""
        yield "out_time_us=250000\n"
        # The child cannot write more until its stderr buffer is emptied.
        if not self._stderr_read.wait(timeout=PIPE_DEADLOCK_TIMEOUT_SECONDS):
            raise AssertionError(
                "stdout was drained to exhaustion without stderr ever being "
                "read — a real FFmpeg would have deadlocked here"
            )
        yield "out_time_us=500000\n"

    def __enter__(self) -> _BlockingPipePopen:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _TimingOutPopen(_FakePopen):
    """A process that never exits, so ``wait`` reports the timeout."""

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 0)


class TestPipeHandling:
    def test_stderr_is_drained_while_the_encode_runs(
        self, logic: MediaSuiteLogic, available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reading stdout to exhaustion first deadlocks a chatty encode."""
        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen",
            lambda command, **_k: _BlockingPipePopen(command),
        )
        reported: list[int] = []

        stderr = logic.run_ffmpeg(["-i", "x", "y"], 1.0, reported.append)

        assert "unsupported frame" in stderr
        assert reported == [25, 50]

    def test_a_process_that_will_not_exit_is_killed(
        self, logic: MediaSuiteLogic, available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leaving the `with` block waits without a timeout — kill first.

        Rule B-02: a wedged encode must surface as a timeout, not park the
        worker thread in ``Popen.__exit__`` forever.
        """
        processes: list[_TimingOutPopen] = []

        def spawn(command: list[str], **_k: object) -> _TimingOutPopen:
            process = _TimingOutPopen(command)
            processes.append(process)
            return process

        monkeypatch.setattr(
            "modules.converters.media_suite.logic.subprocess.Popen", spawn
        )

        with pytest.raises(FFmpegError, match="exceeded"):
            logic.run_ffmpeg(["-i", "x", "y"])

        assert processes[0].killed is True
