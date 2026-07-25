"""Media Suite — business logic layer.

Every operation shells out to FFmpeg, which is the only external binary
OmniForge depends on. It is looked up in ``bundled/`` first so a packaged
build is self-contained, then on PATH.

FFmpeg runs entirely locally; nothing is uploaded (rule C-01). Each
invocation is bounded by a timeout so a wedged encode cannot hang the app
(rule B-02).

Zero NiceGUI imports permitted (rule A-01).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable
from functools import partial
from pathlib import Path
from typing import Any

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.sandbox import SandboxTask, run_reporting_progress
from modules.converters.media_suite.constants import (
    AUDIO_BITRATE_KBPS,
    BITS_PER_KILOBIT,
    BUNDLED_DIR,
    BYTES_PER_MIB,
    CONTAINER_OVERHEAD_FRACTION,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    FFMPEG_BINARY,
    FFMPEG_TIMEOUT_SECONDS,
    FFPROBE_BINARY,
    LOUDNESS_RANGE_LU,
    LOUDNESS_TARGET_LUFS,
    LOUDNESS_TRUE_PEAK_DB,
    MAX_ENCODE_PERCENT,
    MICROSECONDS_PER_SECOND,
    MIN_VIDEO_BITRATE_KBPS,
    MP3_BITRATE,
    OUTPUT_SUBDIR,
    PRESET_TARGET_MB,
    PROGRESS_COMPLETE,
    STREAM_DRAIN_JOIN_SECONDS,
    THUMBNAIL_DIR_TEMPLATE,
    THUMBNAIL_FILENAME_TEMPLATE,
    THUMBNAIL_WIDTH_PX,
    X264_CRF,
    X264_PRESET,
)
from modules.converters.media_suite.models import (
    MediaInfo,
    MediaOperation,
    MediaParams,
    MediaResult,
    SizePreset,
)
from shared.constants import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from shared.platform_info import is_windows
from shared.validators import validate_write_target

logger = get_logger(__name__)

#: Called with 0-100 as an encode advances.
ProgressCallback = Callable[[int], None]

_OUT_TIME_KEY = "out_time_us="


def _parse_out_time_us(line: str) -> int | None:
    """Extract the elapsed output time from one FFmpeg progress line.

    Args:
        line: A line from FFmpeg's ``-progress`` stream.

    Returns:
        Microseconds of output produced so far, or ``None`` when this line
        carries something else (FFmpeg emits a block of keys per update).
    """
    text = line.strip()
    if not text.startswith(_OUT_TIME_KEY):
        return None
    try:
        return max(0, int(text[len(_OUT_TIME_KEY):]))
    except ValueError:
        # "N/A" appears before the first frame is written.
        return None


def _invoke(
    handler: Callable[..., tuple[list[Path], list[str]]],
    source: Path,
    output_dir: Path,
    params: MediaParams,
    report: ProgressCallback,
) -> tuple[list[Path], list[str]]:
    """Call one media handler with a progress reporter attached.

    A module-level function so ``functools.partial`` can bind this iteration's
    source — a closure over the loop variable would make every file re-encode
    the last one.

    Args:
        handler: The operation implementation.
        source: The input file.
        output_dir: Directory to write into.
        params: Operation parameters.
        report: Called with 0-100 as the encode advances.

    Returns:
        ``(outputs, warnings)`` from the handler.
    """
    return handler(source, output_dir, params, report)


def _discard_partial_output(target: Path | None) -> None:
    """Delete the half-written file a failed encode left behind.

    A truncated file is indistinguishable from a finished one to the user, so
    it must not survive the failure that produced it.

    The target is passed in rather than inferred from the command. Reading it
    back as ``arguments[-1]`` happened to be correct for all five handlers, but
    it silently becomes an ``unlink()`` of the wrong path the first time anyone
    appends a trailing flag — a booby trap for a future edit.

    Args:
        target: The file the encode was writing, or ``None`` when there is
            nothing to clean up.
    """
    if target is None:
        return
    # A numbered pattern (thumbnails) is not a single file — leave it alone.
    if "%" in target.name:
        return
    try:
        if target.is_file():
            target.unlink()
            logger.debug("media_suite.partial_removed — path=%s", target)
    except OSError as exc:
        logger.warning("media_suite.partial_cleanup_failed — path=%s", target, exc_info=exc)


class FFmpegError(RuntimeError):
    """Raised when an FFmpeg invocation fails."""


def find_binary(name: str) -> Path | None:
    """Locate an FFmpeg tool, preferring a bundled copy over PATH.

    Args:
        name: Tool name, e.g. ``"ffmpeg"``.

    Returns:
        Path to the executable, or ``None`` when it cannot be found.
    """
    suffix = ".exe" if is_windows() else ""
    bundled = Path(BUNDLED_DIR) / f"{name}{suffix}"
    if bundled.is_file():
        return bundled.resolve()
    found = shutil.which(name)
    return Path(found) if found else None


def ffmpeg_available() -> bool:
    """Return True when both FFmpeg and ffprobe can be located."""
    return find_binary(FFMPEG_BINARY) is not None and (
        find_binary(FFPROBE_BINARY) is not None
    )


class MediaSuiteLogic:
    """Implements every media_suite operation."""

    def __init__(self) -> None:
        self._execution = SandboxTask()
        self._last_result: MediaResult | None = None

    async def register(self) -> None:
        """Subscribe the EventBus execute handler.  Call from ``on_load()``."""
        event_bus.subscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.subscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("media_suite.logic.registered")

    async def unregister(self) -> None:
        """Unsubscribe the EventBus handler.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.unsubscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("media_suite.logic.unregistered")

    # ─── EventBus handler ─────────────────────────────────────────────────────

    async def _on_cancel(self, _payload: Any) -> None:
        """Stop the in-flight operation at the user's request.

        The cancelled run reports nothing itself — this handler owns telling
        the UI, so the execute handler can stay quiet about a deliberate
        user action (RFC 0003).
        """
        if self._execution.request_cancel():
            logger.info("media_suite.cancel_requested")
            await event_bus.publish(EVENT_CANCELLED, None)

    async def _on_execute(self, payload: Any) -> None:
        """Run an operation requested by the UI.

        Args:
            payload: A MediaParams instance.
        """
        if not isinstance(payload, MediaParams):
            logger.error("media_suite.bad_payload — type=%s", type(payload).__name__)
            return

        self._last_result = None
        try:
            # Rule B-02 — bounded and cancellable. Iterating the generator here
            # directly meant the sandbox's timeout applied to nothing (RFC 0003).
            await self._execution.consume(
                self.execute(payload),
                lambda event: event_bus.publish(EVENT_PROGRESS, event),
            )
            if self._last_result is not None:
                await event_bus.publish(EVENT_DONE, self._last_result)
        except TimeoutError:
            logger.warning("media_suite.timeout — after %ds", DEFAULT_EXECUTION_TIMEOUT_SECONDS)
            await event_bus.publish(
                EVENT_ERROR,
                f"The operation exceeded {DEFAULT_EXECUTION_TIMEOUT_SECONDS}s and was stopped.",
            )
        except asyncio.CancelledError:
            # This handler task is itself the cancellation target and the cancel
            # handler has already told the UI, so a deliberate user action is
            # kept out of the error log.
            logger.info("media_suite.cancelled")
        except Exception as exc:
            logger.error("media_suite.execute_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    # ─── Dispatch ─────────────────────────────────────────────────────────────

    async def execute(self, params: MediaParams) -> AsyncIterator[ProgressEvent]:
        """Process every input file, reporting progress throughout.

        Args:
            params: Validated operation parameters.

        Yields:
            ProgressEvent at each checkpoint.
        """
        yield ProgressEvent(percent=0, message="Checking FFmpeg…")

        if not ffmpeg_available():
            raise FFmpegError(
                "FFmpeg is not available. Install it and restart OmniForge."
            )

        missing = [p for p in params.input_paths if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"File not found: {missing[0]}")

        # Rule B-07 — confine writes to exports/, temp/, or the directory the
        # user chose for this run. Resolving here also stops a crafted filename
        # from escaping that directory via traversal.
        output_dir = validate_write_target(
            params.output_dir / OUTPUT_SUBDIR, extra_roots=(params.output_dir,)
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        handler = self._handlers()[params.operation]
        outputs: list[Path] = []
        warnings: list[str] = []
        total = len(params.input_paths)

        for index, source in enumerate(params.input_paths, start=1):
            yield ProgressEvent(
                percent=int((index - 1) / total * PROGRESS_COMPLETE),
                message=f"Processing {index}/{total}: {source.name}…",
            )
            file_base = int((index - 1) / total * PROGRESS_COMPLETE)
            file_span = PROGRESS_COMPLETE / total

            async def publish(encode_percent: int, base: int = file_base,
                              span: float = file_span, name: str = source.name) -> None:
                """Map one file's encode progress onto the run's overall bar."""
                await event_bus.publish(
                    EVENT_PROGRESS,
                    ProgressEvent(
                        percent=min(base + int(encode_percent * span / 100), PROGRESS_COMPLETE),
                        message=f"{name} — {encode_percent}%",
                    ),
                )

            # partial binds this iteration's source; a bare lambda would close
            # over the loop variable and every file would re-encode the last one.
            produced, notes = await run_reporting_progress(
                partial(_invoke, handler, source, output_dir, params), publish
            )
            outputs.extend(produced)
            warnings.extend(notes)

        self._last_result = MediaResult(
            operation=params.operation,
            output_paths=outputs,
            files_processed=total,
            input_bytes=sum(p.stat().st_size for p in params.input_paths),
            output_bytes=sum(self._path_size(p) for p in outputs),
            detail=self._summarise(params.operation, outputs, total),
            warnings=warnings,
        )
        logger.info(
            "media_suite.done — op=%s files=%d outputs=%d",
            params.operation.value,
            total,
            len(outputs),
        )
        yield ProgressEvent(
            percent=PROGRESS_COMPLETE,
            message=self._last_result.detail,
            output_path=outputs[0] if outputs else None,
        )

    def _handlers(
        self,
    ) -> dict[
        MediaOperation,
        Callable[..., tuple[list[Path], list[str]]],
    ]:
        """Map each operation to its implementation."""
        return {
            MediaOperation.COMPRESS_VIDEO: self._compress_video,
            MediaOperation.EXTRACT_AUDIO: self._extract_audio,
            MediaOperation.TO_MP4: self._to_mp4,
            MediaOperation.THUMBNAILS: self._thumbnails,
            MediaOperation.NORMALIZE_AUDIO: self._normalize_audio,
        }

    # ─── Operations ───────────────────────────────────────────────────────────

    def _compress_video(
        self,
        source: Path,
        output_dir: Path,
        params: MediaParams,
        report: ProgressCallback | None = None,
    ) -> tuple[list[Path], list[str]]:
        """Re-encode a video to fit under a target file size.

        The video bitrate is derived from the target size and the clip's
        duration, so the result lands near the ceiling rather than being
        arbitrarily over- or under-compressed.
        """
        target = output_dir / f"{source.stem}_compressed.mp4"
        info = self.probe(source)
        target_mb = self.target_megabytes(params)
        notes: list[str] = []

        bitrate = self.video_bitrate_kbps(target_mb, info.duration_seconds)
        if bitrate is None:
            notes.append(
                f"{source.name}: duration unknown, so a constant-quality encode "
                "was used instead of a size-targeted one."
            )
            arguments = ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", str(X264_CRF)]
        else:
            if bitrate == MIN_VIDEO_BITRATE_KBPS:
                notes.append(
                    f"{source.name}: {target_mb} MB is very small for "
                    f"{info.duration_seconds:.0f}s — encoded at the minimum bitrate."
                )
            arguments = [
                "-c:v", "libx264", "-preset", X264_PRESET,
                "-b:v", f"{bitrate}k",
                "-maxrate", f"{bitrate}k",
                "-bufsize", f"{bitrate * 2}k",
            ]

        audio = ["-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_KBPS}k"] if info.has_audio else ["-an"]
        self.run_ffmpeg(
            ["-i", str(source), *arguments, *audio, str(target)],
            info.duration_seconds,
            report,
        )

        if target.is_file() and target.stat().st_size > target_mb * BYTES_PER_MIB:
            notes.append(
                f"{source.name}: the encode came out above {target_mb} MB. "
                "Try a lower target or a shorter clip."
            )
        return [target], notes

    def _extract_audio(
        self,
        source: Path,
        output_dir: Path,
        _params: MediaParams,
        report: ProgressCallback | None = None,
    ) -> tuple[list[Path], list[str]]:
        """Pull the audio track out of a video as MP3."""
        info = self.probe(source)
        if not info.has_audio:
            return [], [f"{source.name} has no audio track — nothing to extract."]

        target = output_dir / f"{source.stem}.mp3"
        self.run_ffmpeg(
            ["-i", str(source), "-vn", "-c:a", "libmp3lame", "-b:a", MP3_BITRATE,
             str(target)],
            info.duration_seconds,
            report,
        )
        return [target], []

    def _to_mp4(
        self,
        source: Path,
        output_dir: Path,
        _params: MediaParams,
        report: ProgressCallback | None = None,
    ) -> tuple[list[Path], list[str]]:
        """Remux or transcode any supported video into MP4."""
        target = output_dir / f"{source.stem}.mp4"
        info = self.probe(source)
        audio = ["-c:a", "aac", "-b:a", f"{AUDIO_BITRATE_KBPS}k"] if info.has_audio else ["-an"]
        self.run_ffmpeg(
            ["-i", str(source), "-c:v", "libx264", "-preset", X264_PRESET,
             "-crf", str(X264_CRF), *audio, "-movflags", "+faststart", str(target)],
            info.duration_seconds,
            report,
        )
        return [target], []

    def _thumbnails(
        self,
        source: Path,
        output_dir: Path,
        params: MediaParams,
        report: ProgressCallback | None = None,
    ) -> tuple[list[Path], list[str]]:
        """Extract stills spread evenly across a video."""
        folder = output_dir / THUMBNAIL_DIR_TEMPLATE.format(stem=source.stem)
        folder.mkdir(parents=True, exist_ok=True)

        info = self.probe(source)
        pattern = folder / THUMBNAIL_FILENAME_TEMPLATE.format(stem=source.stem)
        notes: list[str] = []

        if info.duration_seconds > 0:
            # One frame every N seconds gives an even spread across the clip.
            interval = info.duration_seconds / params.thumbnail_count
            video_filter = f"fps=1/{interval:.6f},scale={THUMBNAIL_WIDTH_PX}:-2"
        else:
            notes.append(
                f"{source.name}: duration unknown, so thumbnails were taken "
                "from the start of the clip."
            )
            video_filter = f"scale={THUMBNAIL_WIDTH_PX}:-2"

        self.run_ffmpeg(
            ["-i", str(source), "-vf", video_filter,
             "-frames:v", str(params.thumbnail_count),
             "-fps_mode", "vfr", str(pattern)]
        )
        return [folder], notes

    def _normalize_audio(
        self,
        source: Path,
        output_dir: Path,
        _params: MediaParams,
        report: ProgressCallback | None = None,
    ) -> tuple[list[Path], list[str]]:
        """Bring a file to the EBU R128 loudness target.

        Uses FFmpeg's ``loudnorm`` filter, which is the standard broadcast
        normaliser — it targets perceived loudness rather than peak level.
        """
        target = output_dir / f"{source.stem}_normalized{source.suffix}"
        loudnorm = (
            f"loudnorm=I={LOUDNESS_TARGET_LUFS}"
            f":TP={LOUDNESS_TRUE_PEAK_DB}"
            f":LRA={LOUDNESS_RANGE_LU}"
        )
        info = self.probe(source)
        self.run_ffmpeg(
            ["-i", str(source), "-af", loudnorm, str(target)],
            info.duration_seconds,
            report,
        )
        return [target], []

    # ─── FFmpeg plumbing ──────────────────────────────────────────────────────

    def run_ffmpeg(
        self,
        arguments: list[str],
        duration_seconds: float = 0.0,
        report: ProgressCallback | None = None,
        target: Path | None = None,
    ) -> str:
        """Invoke FFmpeg with the given arguments, relaying its progress.

        FFmpeg is asked for a machine-readable progress stream on stdout
        (``-progress pipe:1``) and each ``out_time_us`` line is turned into a
        percentage against the clip's known duration. Without this an encode
        emitted a single event per *file*, so a ten-minute conversion showed a
        frozen bar for ten minutes (rule D-08).

        Args:
            arguments: Arguments following the executable. ``-y`` and
                ``-hide_banner`` are added automatically.
            duration_seconds: The clip's length, used to turn elapsed output
                time into a percentage. Zero means progress cannot be scaled and
                only a heartbeat is reported.
            report: Called with 0-100 as the encode advances.
            target: The file being written, removed if the encode fails.
                Defaults to the command's last argument, which is where every
                handler here puts it.

        Returns:
            FFmpeg's stderr, which is where it reports errors.

        Raises:
            FFmpegError: When FFmpeg is missing, fails, or exceeds its timeout.
        """
        binary = find_binary(FFMPEG_BINARY)
        if binary is None:
            raise FFmpegError("FFmpeg is not available.")

        output = target if target is not None else (
            Path(arguments[-1]) if arguments else None
        )

        command = [
            str(binary), "-hide_banner", "-loglevel", "error", "-y",
            "-progress", "pipe:1", "-nostats", *arguments,
        ]
        try:
            with subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as process:
                try:
                    stderr = self._pump_progress(process, duration_seconds, report)
                except BaseException:
                    # Leaving the `with` block calls Popen.__exit__, which waits
                    # for the child *without* a timeout. A still-running FFmpeg
                    # would therefore hang the worker thread forever instead of
                    # surfacing the timeout. Kill it on the way out.
                    process.kill()
                    raise
        except subprocess.TimeoutExpired as exc:
            _discard_partial_output(output)
            raise FFmpegError(
                f"FFmpeg exceeded {FFMPEG_TIMEOUT_SECONDS}s and was stopped."
            ) from exc
        except OSError as exc:
            raise FFmpegError(f"FFmpeg could not be started: {exc}") from exc

        if process.returncode != 0:
            # A failed or killed encode leaves a truncated file behind that
            # looks like a successful output. Remove it before reporting.
            _discard_partial_output(output)
            detail = stderr.strip().splitlines()
            raise FFmpegError(
                "FFmpeg failed: " + (detail[-1] if detail else "no error output.")
            )
        return stderr

    def _pump_progress(
        self,
        process: subprocess.Popen[str],
        duration_seconds: float,
        report: ProgressCallback | None,
    ) -> str:
        """Drain FFmpeg's progress stream until it exits, returning stderr.

        Both pipes are drained *concurrently*. Reading only stdout here and
        leaving stderr until after the process exits deadlocks the encode as
        soon as FFmpeg writes more than the operating system's pipe buffer
        (~64 KB) to stderr: FFmpeg blocks on the full stderr pipe, and this
        thread blocks reading a stdout that will never advance. A file that
        provokes many decoder warnings hits that reliably. A reader thread on
        stderr keeps it empty, so neither side can wedge.

        Args:
            process: The running FFmpeg process.
            duration_seconds: Clip length for scaling, or 0 when unknown.
            report: Called with 0-100 as the encode advances.

        Returns:
            Everything FFmpeg wrote to stderr.

        Raises:
            subprocess.TimeoutExpired: When the encode outlives its budget.
        """
        deadline = time.monotonic() + FFMPEG_TIMEOUT_SECONDS
        captured: list[str] = []
        drain = self._start_stderr_drain(process, captured)

        try:
            if process.stdout is not None:
                for line in process.stdout:
                    if time.monotonic() > deadline:
                        process.kill()
                        raise subprocess.TimeoutExpired(
                            process.args, FFMPEG_TIMEOUT_SECONDS
                        )
                    if report is None or duration_seconds <= 0:
                        continue
                    micros = _parse_out_time_us(line)
                    if micros is None:
                        continue
                    elapsed = micros / MICROSECONDS_PER_SECOND
                    report(
                        min(
                            int(elapsed / duration_seconds * PROGRESS_COMPLETE),
                            MAX_ENCODE_PERCENT,
                        )
                    )
            process.wait(timeout=max(1.0, deadline - time.monotonic()))
        finally:
            # The child is gone (exited or killed) by every path that reaches
            # here, so its stderr is at EOF and the drain finishes promptly.
            if drain is not None:
                drain.join(timeout=STREAM_DRAIN_JOIN_SECONDS)

        return "".join(captured)

    def _start_stderr_drain(
        self, process: subprocess.Popen[str], sink: list[str]
    ) -> threading.Thread | None:
        """Start a daemon thread that empties *process*'s stderr into *sink*.

        Args:
            process: The running FFmpeg process.
            sink: Collects the captured text; appended to from the thread and
                read only after it has been joined.

        Returns:
            The reader thread, or ``None`` when stderr was not piped.
        """
        stream = process.stderr
        if stream is None:
            return None

        def _drain() -> None:
            """Read stderr to EOF, tolerating a pipe closed under us."""
            try:
                sink.append(stream.read())
            except (OSError, ValueError) as exc:
                # The pipe was closed while we were reading — the process was
                # killed. Whatever it managed to say is already in *sink*.
                logger.debug("media_suite.stderr_drain_ended — reason=%s", exc)

        thread = threading.Thread(target=_drain, daemon=True, name="ffmpeg-stderr")
        thread.start()
        return thread

    def probe(self, source: Path) -> MediaInfo:
        """Read duration, dimensions and stream layout from a media file.

        A probe failure is not fatal: the operations fall back to
        duration-independent behaviour and say so.

        Args:
            source: The media file.

        Returns:
            What could be determined, with zeros where it could not.
        """
        binary = find_binary(FFPROBE_BINARY)
        if binary is None:
            return MediaInfo()

        command = [
            str(binary), "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(source),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True,
                timeout=FFMPEG_TIMEOUT_SECONDS, check=False,
            )
            if completed.returncode != 0:
                return MediaInfo()
            payload = json.loads(completed.stdout or "{}")
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
            logger.warning("media_suite.probe_failed — file=%s reason=%s", source.name, exc)
            return MediaInfo()

        return self._parse_probe(payload)

    def _parse_probe(self, payload: dict[str, Any]) -> MediaInfo:
        """Convert ffprobe's JSON into a MediaInfo.

        Args:
            payload: The parsed ffprobe output.

        Returns:
            The extracted media facts.
        """
        streams: list[dict[str, Any]] = payload.get("streams") or []
        video: dict[str, Any] = next(
            (s for s in streams if s.get("codec_type") == "video"), {}
        )
        has_audio = any(s.get("codec_type") == "audio" for s in streams)

        duration = 0.0
        raw_duration = (payload.get("format") or {}).get("duration")
        if raw_duration is not None:
            try:
                duration = max(0.0, float(raw_duration))
            except (TypeError, ValueError):
                duration = 0.0

        return MediaInfo(
            duration_seconds=duration,
            width=int(video.get("width") or 0),
            height=int(video.get("height") or 0),
            has_audio=has_audio,
        )

    # ─── Calculations ─────────────────────────────────────────────────────────

    def target_megabytes(self, params: MediaParams) -> int:
        """Resolve the size ceiling for a compression run.

        Args:
            params: The operation parameters.

        Returns:
            The ceiling in mebibytes.
        """
        if params.preset is SizePreset.CUSTOM:
            return params.target_mb
        return PRESET_TARGET_MB[params.preset.value]

    def video_bitrate_kbps(self, target_mb: int, duration_seconds: float) -> int | None:
        """Derive the video bitrate that lands a clip near *target_mb*.

        Subtracts the audio track and a container-overhead allowance from the
        budget before dividing by duration.

        Args:
            target_mb: Size ceiling in mebibytes.
            duration_seconds: Clip length.

        Returns:
            The bitrate in kbps, floored at a usable minimum, or ``None`` when
            the duration is unknown and no calculation is possible.
        """
        if duration_seconds <= 0:
            return None

        total_kilobits = (target_mb * BYTES_PER_MIB * 8) / BITS_PER_KILOBIT
        usable = total_kilobits * (1 - CONTAINER_OVERHEAD_FRACTION)
        video_kilobits = usable - (AUDIO_BITRATE_KBPS * duration_seconds)
        return max(MIN_VIDEO_BITRATE_KBPS, int(video_kilobits / duration_seconds))

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _path_size(self, path: Path) -> int:
        """Return the size of a file, or of every file in a directory.

        Args:
            path: File or directory to measure.

        Returns:
            Size in bytes; 0 when the path is absent.
        """
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return 0

    def _summarise(
        self, operation: MediaOperation, outputs: list[Path], files: int
    ) -> str:
        """Build the completion message shown in the UI.

        Args:
            operation: The operation that ran.
            outputs: Files or folders produced.
            files: Number of source files handled.

        Returns:
            A short human-readable summary.
        """
        label = operation.value.replace("_", " ").title()
        noun = "file" if files == 1 else "files"
        return f"{label} complete — {files} {noun} → {len(outputs)} output(s)"
