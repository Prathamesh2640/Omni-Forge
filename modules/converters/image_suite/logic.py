"""Image Suite — business logic layer.

Raster work runs through Pillow (with pillow-heif registered for HEIC/HEIF)
and SVG rasterisation through svglib + reportlab, so nothing here needs an
external binary or a network connection.

Zero NiceGUI imports permitted (rule A-01).
"""
from __future__ import annotations

import asyncio
import io
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from PIL import Image

from core.event_bus import event_bus
from core.logger import get_logger
from core.models import ProgressEvent
from core.sandbox import SandboxTask, run_in_thread
from modules.converters.image_suite.constants import (
    ANDROID_DENSITIES,
    ANDROID_DRAWABLE_DIR_TEMPLATE,
    BYTES_PER_KIB,
    EVENT_CANCEL,
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_EXECUTE,
    EVENT_PROGRESS,
    FAVICON_DIR_TEMPLATE,
    FAVICON_FILENAME_TEMPLATE,
    FAVICON_ICO_NAME,
    FAVICON_ICO_SIZES,
    FAVICON_SIZES,
    FLATTEN_BACKGROUND,
    HEIF_EXTENSIONS,
    MAX_QUALITY,
    MIN_QUALITY,
    OPAQUE_FORMATS,
    OUTPUT_SUBDIR,
    PROGRESS_COMPLETE,
    STRIPPED_INFO_KEYS,
    SVG_DEFAULT_VIEWPORT,
    TARGET_SIZE_MAX_ATTEMPTS,
    TARGET_SIZE_TOLERANCE,
    VECTOR_DEFAULT_FILL,
    VECTOR_DRAWABLE_TEMPLATE,
    VECTOR_PATH_TEMPLATE,
)
from modules.converters.image_suite.models import (
    ImageOperation,
    ImageParams,
    ImageResult,
)
from shared.constants import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from shared.validators import validate_write_target

logger = get_logger(__name__)

_SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
_HEIF_REGISTERED = False


def register_heif() -> bool:
    """Teach Pillow to open HEIC/HEIF files, once per process.

    Returns:
        True when the opener is available.
    """
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return True
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        logger.warning("image_suite.heif_unavailable")
        return False
    _HEIF_REGISTERED = True
    return True


class ImageSuiteLogic:
    """Implements every image_suite operation."""

    def __init__(self) -> None:
        self._execution = SandboxTask()
        self._last_result: ImageResult | None = None

    async def register(self) -> None:
        """Subscribe the EventBus execute handler.  Call from ``on_load()``."""
        event_bus.subscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.subscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("image_suite.logic.registered")

    async def unregister(self) -> None:
        """Unsubscribe the EventBus handler.  Call from ``on_unload()``."""
        event_bus.unsubscribe(EVENT_EXECUTE, self._on_execute)
        event_bus.unsubscribe(EVENT_CANCEL, self._on_cancel)
        logger.debug("image_suite.logic.unregistered")

    # ─── EventBus handler ─────────────────────────────────────────────────────

    async def _on_cancel(self, _payload: Any) -> None:
        """Stop the in-flight operation at the user's request.

        The cancelled run reports nothing itself — this handler owns telling
        the UI, so the execute handler can stay quiet about a deliberate
        user action (RFC 0003).
        """
        if self._execution.request_cancel():
            logger.info("image_suite.cancel_requested")
            await event_bus.publish(EVENT_CANCELLED, None)

    async def _on_execute(self, payload: Any) -> None:
        """Run an operation requested by the UI.

        Args:
            payload: An ImageParams instance.
        """
        if not isinstance(payload, ImageParams):
            logger.error("image_suite.bad_payload — type=%s", type(payload).__name__)
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
            logger.warning("image_suite.timeout — after %ds", DEFAULT_EXECUTION_TIMEOUT_SECONDS)
            await event_bus.publish(
                EVENT_ERROR,
                f"The operation exceeded {DEFAULT_EXECUTION_TIMEOUT_SECONDS}s and was stopped.",
            )
        except asyncio.CancelledError:
            # This handler task is itself the cancellation target and the cancel
            # handler has already told the UI, so a deliberate user action is
            # kept out of the error log.
            logger.info("image_suite.cancelled")
        except Exception as exc:
            logger.error("image_suite.execute_failed", exc_info=exc)
            await event_bus.publish(EVENT_ERROR, str(exc))

    # ─── Dispatch ─────────────────────────────────────────────────────────────

    async def execute(self, params: ImageParams) -> AsyncIterator[ProgressEvent]:
        """Process every input image, reporting progress throughout.

        Args:
            params: Validated operation parameters.

        Yields:
            ProgressEvent at each checkpoint.
        """
        yield ProgressEvent(percent=0, message="Validating input…")

        missing = [p for p in params.input_paths if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"File not found: {missing[0]}")

        if any(p.suffix.lower() in HEIF_EXTENSIONS for p in params.input_paths):
            register_heif()

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
            produced, notes = await run_in_thread(handler, source, output_dir, params)
            outputs.extend(produced)
            warnings.extend(notes)
            yield ProgressEvent(
                percent=int(index / total * PROGRESS_COMPLETE),
                message=f"Processed {index}/{total}: {source.name}",
            )

        self._last_result = ImageResult(
            operation=params.operation,
            output_paths=outputs,
            images_processed=total,
            input_bytes=sum(p.stat().st_size for p in params.input_paths),
            output_bytes=sum(self._path_size(p) for p in outputs),
            detail=self._summarise(params.operation, outputs, total),
            warnings=warnings,
        )
        logger.info(
            "image_suite.done — op=%s images=%d outputs=%d",
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
        ImageOperation,
        Callable[[Path, Path, ImageParams], tuple[list[Path], list[str]]],
    ]:
        """Map each operation to its implementation."""
        return {
            ImageOperation.CONVERT: self._convert,
            ImageOperation.COMPRESS_TO_TARGET: self._compress_to_target,
            ImageOperation.RESIZE: self._resize,
            ImageOperation.STRIP_METADATA: self._strip_metadata,
            ImageOperation.SVG_TO_DENSITIES: self._svg_to_densities,
            ImageOperation.SVG_TO_FAVICONS: self._svg_to_favicons,
            ImageOperation.SVG_TO_VECTOR_DRAWABLE: self._svg_to_vector_drawable,
        }

    # ─── Raster operations ────────────────────────────────────────────────────

    def _convert(
        self, source: Path, output_dir: Path, params: ImageParams
    ) -> tuple[list[Path], list[str]]:
        """Re-encode an image into the selected format."""
        target = output_dir / (source.stem + params.output_extension)
        with Image.open(source) as image:
            prepared = self._prepare(image, params.pillow_format)
            self._save(prepared, target, params.pillow_format, params.quality)
        return [target], []

    def _resize(
        self, source: Path, output_dir: Path, params: ImageParams
    ) -> tuple[list[Path], list[str]]:
        """Scale an image so its longest edge matches the requested size.

        Images already smaller than the limit are copied unchanged rather than
        upscaled, which would only add bytes without adding detail.
        """
        target = output_dir / (source.stem + params.output_extension)
        with Image.open(source) as image:
            prepared = self._prepare(image, params.pillow_format)
            longest = max(prepared.width, prepared.height)
            notes: list[str] = []
            if longest > params.max_dimension:
                scale = params.max_dimension / longest
                prepared = prepared.resize(
                    (max(1, round(prepared.width * scale)),
                     max(1, round(prepared.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            else:
                notes.append(
                    f"{source.name} is already {longest}px — left at its original size."
                )
            self._save(prepared, target, params.pillow_format, params.quality)
        return [target], notes

    def _compress_to_target(
        self, source: Path, output_dir: Path, params: ImageParams
    ) -> tuple[list[Path], list[str]]:
        """Binary-search encoder quality to land under a target file size.

        Each trial encodes into memory, so only the chosen result is ever
        written to disk.
        """
        target = output_dir / (source.stem + params.output_extension)
        target_bytes = params.target_kb * BYTES_PER_KIB
        fmt = params.pillow_format

        with Image.open(source) as image:
            prepared = self._prepare(image, fmt)

            low, high = MIN_QUALITY, MAX_QUALITY
            best: bytes | None = None
            best_quality = MIN_QUALITY

            for _ in range(TARGET_SIZE_MAX_ATTEMPTS):
                quality = (low + high) // 2
                encoded = self._encode(prepared, fmt, quality)

                if len(encoded) <= target_bytes:
                    best, best_quality = encoded, quality
                    if len(encoded) >= target_bytes * (1 - TARGET_SIZE_TOLERANCE):
                        break
                    low = quality + 1
                else:
                    high = quality - 1
                if low > high:
                    break

            notes: list[str] = []
            if best is None:
                best = self._encode(prepared, fmt, MIN_QUALITY)
                best_quality = MIN_QUALITY
                notes.append(
                    f"{source.name}: could not reach {params.target_kb} KB even at "
                    f"minimum quality — wrote the smallest version "
                    f"({len(best) // BYTES_PER_KIB} KB)."
                )

        target.write_bytes(best)
        logger.debug(
            "image_suite.compressed — file=%s quality=%d bytes=%d",
            source.name,
            best_quality,
            len(best),
        )
        return [target], notes

    def _strip_metadata(
        self, source: Path, output_dir: Path, params: ImageParams
    ) -> tuple[list[Path], list[str]]:
        """Write a copy carrying no EXIF, ICC or XMP metadata.

        Re-encoding through a fresh image object is what actually drops the
        metadata; copying the file would preserve it.
        """
        target = output_dir / (source.stem + params.output_extension)
        with Image.open(source) as image:
            prepared = self._prepare(image, params.pillow_format)
            # Pasting into a fresh image carries the pixels but none of the
            # source's info dict, which is where EXIF/ICC/XMP travel.
            clean = Image.new(prepared.mode, prepared.size)
            clean.paste(prepared)
            for key in STRIPPED_INFO_KEYS:
                clean.info.pop(key, None)
            self._save(clean, target, params.pillow_format, params.quality)
        return [target], []

    # ─── SVG operations ───────────────────────────────────────────────────────

    def _svg_to_densities(
        self, source: Path, output_dir: Path, params: ImageParams
    ) -> tuple[list[Path], list[str]]:
        """Rasterise an SVG into every Android density bucket."""
        produced: list[Path] = []
        for density, scale in ANDROID_DENSITIES.items():
            bucket = output_dir / ANDROID_DRAWABLE_DIR_TEMPLATE.format(density=density)
            bucket.mkdir(parents=True, exist_ok=True)
            size = max(1, round(params.base_size_dp * scale))
            target = bucket / f"{source.stem}.png"
            self._rasterise(source, target, size)
            produced.append(target)
        return produced, []

    def _svg_to_favicons(
        self, source: Path, output_dir: Path, _params: ImageParams
    ) -> tuple[list[Path], list[str]]:
        """Rasterise an SVG into the standard favicon sizes plus a .ico bundle."""
        folder = output_dir / FAVICON_DIR_TEMPLATE.format(stem=source.stem)
        folder.mkdir(parents=True, exist_ok=True)

        for size in FAVICON_SIZES:
            self._rasterise(
                source, folder / FAVICON_FILENAME_TEMPLATE.format(size=size), size
            )

        # A single .ico holding several resolutions is what browsers expect.
        largest = max(FAVICON_ICO_SIZES)
        with Image.open(
            folder / FAVICON_FILENAME_TEMPLATE.format(size=largest)
        ) as base:
            base.save(
                folder / FAVICON_ICO_NAME,
                format="ICO",
                sizes=[(s, s) for s in FAVICON_ICO_SIZES],
            )
        return [folder], []

    def _svg_to_vector_drawable(
        self, source: Path, output_dir: Path, _params: ImageParams
    ) -> tuple[list[Path], list[str]]:
        """Translate an SVG's paths into an Android VectorDrawable.

        Handles the subset Android supports: ``<path>`` elements with solid
        fills. Gradients, filters and embedded rasters have no VectorDrawable
        equivalent and are reported rather than silently dropped.
        """
        target = output_dir / f"{source.stem}.xml"
        root = ElementTree.fromstring(source.read_text(encoding="utf-8"))

        width, height = self._svg_viewport(root)
        paths: list[str] = []
        notes: list[str] = []

        for element in root.iter():
            tag = element.tag.replace(_SVG_NAMESPACE, "")
            if tag == "path":
                data = element.get("d")
                if data:
                    paths.append(
                        VECTOR_PATH_TEMPLATE.format(
                            fill=self._android_colour(element.get("fill")),
                            data=data.replace('"', "'").strip(),
                        )
                    )
            elif tag in {"linearGradient", "radialGradient", "filter", "image"}:
                notes.append(
                    f"{source.name}: <{tag}> has no VectorDrawable equivalent "
                    "and was skipped."
                )

        if not paths:
            notes.append(
                f"{source.name}: no <path> elements found. Shapes such as "
                "<rect> and <circle> must be converted to paths first."
            )

        target.write_text(
            VECTOR_DRAWABLE_TEMPLATE.format(
                width=int(width),
                height=int(height),
                viewport_width=width,
                viewport_height=height,
                paths="".join(paths),
            ),
            encoding="utf-8",
        )
        return [target], notes

    def _svg_viewport(self, root: ElementTree.Element) -> tuple[float, float]:
        """Determine an SVG's viewport dimensions.

        Args:
            root: The parsed ``<svg>`` element.

        Returns:
            A ``(width, height)`` pair, defaulting to 24x24 when absent.
        """
        view_box = root.get("viewBox")
        if view_box:
            parts = re.split(r"[\s,]+", view_box.strip())
            if len(parts) == 4:
                try:
                    return float(parts[2]), float(parts[3])
                except ValueError:
                    logger.debug("image_suite.bad_viewbox — value=%s", view_box)

        return (
            self._svg_length(root.get("width")),
            self._svg_length(root.get("height")),
        )

    def _svg_length(self, raw: str | None) -> float:
        """Parse an SVG length, discarding any unit suffix.

        Args:
            raw: The attribute value, e.g. ``"24"`` or ``"24px"``.

        Returns:
            The numeric value, or the default viewport size.
        """
        if not raw:
            return SVG_DEFAULT_VIEWPORT
        match = re.match(r"^\s*([0-9.]+)", raw)
        return float(match.group(1)) if match else SVG_DEFAULT_VIEWPORT

    def _android_colour(self, fill: str | None) -> str:
        """Convert an SVG fill into an Android ``#AARRGGBB`` colour.

        Args:
            fill: The SVG ``fill`` attribute value.

        Returns:
            An Android colour string; opaque black when unset or unsupported.
        """
        if not fill or fill in {"none", "currentColor"}:
            return VECTOR_DEFAULT_FILL
        text = fill.strip()
        if not text.startswith("#"):
            return VECTOR_DEFAULT_FILL

        digits = text[1:]
        if len(digits) == 3:  # #RGB shorthand
            digits = "".join(c * 2 for c in digits)
        if len(digits) == 6:
            return f"#FF{digits.upper()}"
        if len(digits) == 8:
            return f"#{digits.upper()}"
        return VECTOR_DEFAULT_FILL

    def _rasterise(self, source: Path, target: Path, size: int) -> None:
        """Render an SVG to a square PNG of *size* pixels.

        Goes SVG → PDF (svglib + reportlab) → PNG (PyMuPDF). reportlab's own
        raster backend needs the Cairo system library, which this project
        deliberately avoids; PyMuPDF is a self-contained wheel that is already
        a dependency.

        The artwork is scaled to fit and centred on a transparent square, so a
        non-square icon is letterboxed rather than stretched.

        Args:
            source: The SVG file.
            target: Destination PNG.
            size: Width and height in pixels.

        Raises:
            ValueError: When the SVG cannot be rendered.
        """
        import fitz
        from reportlab.graphics import renderPDF
        from svglib.svglib import svg2rlg

        drawing = svg2rlg(str(source))
        if drawing is None:
            raise ValueError(f"{source.name} could not be parsed as SVG.")

        pdf_bytes = renderPDF.drawToString(drawing)
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            page = document[0]
            width, height = page.rect.width, page.rect.height
            if not width or not height:
                raise ValueError(f"{source.name} has no drawable area.")

            zoom = size / max(width, height)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
            rendered = Image.frombytes(
                "RGBA", (pixmap.width, pixmap.height), pixmap.samples
            )

        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(
            rendered,
            ((size - rendered.width) // 2, (size - rendered.height) // 2),
            rendered,
        )
        canvas.save(target, format="PNG")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _prepare(self, image: Image.Image, target_format: str) -> Image.Image:
        """Convert an image into a mode the target format can store.

        Args:
            image: The opened source image.
            target_format: Pillow format name being written.

        Returns:
            An image ready to save, flattened onto white where the format
            cannot carry transparency.
        """
        if target_format in OPAQUE_FORMATS:
            if image.mode in {"RGBA", "LA", "P"}:
                converted = image.convert("RGBA")
                canvas = Image.new("RGB", converted.size, FLATTEN_BACKGROUND)
                canvas.paste(converted, mask=converted.split()[-1])
                return canvas
            return image.convert("RGB")
        if image.mode == "P":
            return image.convert("RGBA")
        return image.copy()

    def _save(
        self, image: Image.Image, target: Path, fmt: str, quality: int
    ) -> None:
        """Write an image, applying quality only where the format uses it.

        Args:
            image: The image to write.
            target: Destination path.
            fmt: Pillow format name.
            quality: Encoder quality.
        """
        options: dict[str, Any] = {"format": fmt}
        if fmt != "PNG":
            options["quality"] = quality
        else:
            options["optimize"] = True
        image.save(target, **options)

    def _encode(self, image: Image.Image, fmt: str, quality: int) -> bytes:
        """Encode an image in memory, for size trials.

        Args:
            image: The image to encode.
            fmt: Pillow format name.
            quality: Encoder quality.

        Returns:
            The encoded bytes.
        """
        buffer = io.BytesIO()
        options: dict[str, Any] = {"format": fmt}
        if fmt != "PNG":
            options["quality"] = quality
        else:
            options["optimize"] = True
        image.save(buffer, **options)
        return buffer.getvalue()

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
        self, operation: ImageOperation, outputs: list[Path], images: int
    ) -> str:
        """Build the completion message shown in the UI.

        Args:
            operation: The operation that ran.
            outputs: Files or folders produced.
            images: Number of source images handled.

        Returns:
            A short human-readable summary.
        """
        label = operation.value.replace("_", " ").title()
        noun = "image" if images == 1 else "images"
        return f"{label} complete — {images} {noun} → {len(outputs)} output(s)"
