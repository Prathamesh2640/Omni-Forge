"""Unit tests for the image_suite logic layer.

Real images are generated with Pillow and the outputs are reopened, so a
corrupt result fails rather than merely existing on disk.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest
from PIL import Image

from core.models import ProgressEvent
from modules.converters.image_suite.constants import (
    ANDROID_DENSITIES,
    BYTES_PER_KIB,
    FAVICON_ICO_NAME,
    FAVICON_SIZES,
    OUTPUT_SUBDIR,
    PROGRESS_COMPLETE,
    VECTOR_DEFAULT_FILL,
)
from modules.converters.image_suite.logic import (
    ImageSuiteLogic,
    register_heif,
)
from modules.converters.image_suite.models import (
    ImageOperation,
    ImageParams,
    ImageResult,
    OutputFormat,
)

SQUARE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path d="M4 4 H20 V20 H4 Z" fill="#FF5733"/>'
    "</svg>"
)


@pytest.fixture()
def logic() -> ImageSuiteLogic:
    """Fresh logic instance with no EventBus registrations."""
    return ImageSuiteLogic()


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    """Directory operations write into."""
    return tmp_path / "exports"


def make_image(
    path: Path, size: tuple[int, int] = (200, 120), mode: str = "RGB"
) -> Path:
    """Write a small test image and return its path."""
    image = Image.new(mode, size, (200, 60, 40) if mode == "RGB" else (200, 60, 40, 128))
    # Varied pixels keep the encoder from producing a degenerate file.
    for x in range(0, size[0], 7):
        for y in range(0, size[1], 5):
            image.putpixel((x, y), (10, 200, 90) if mode == "RGB" else (10, 200, 90, 255))
    image.save(path)
    return path


def make_svg(path: Path, content: str = SQUARE_SVG) -> Path:
    """Write an SVG file and return its path."""
    path.write_text(content, encoding="utf-8")
    return path


def run(
    logic: ImageSuiteLogic, params: ImageParams
) -> tuple[list[ProgressEvent], ImageResult]:
    """Execute an operation to completion."""

    async def drive() -> list[ProgressEvent]:
        return [event async for event in logic.execute(params)]

    events = asyncio.run(drive())
    assert logic._last_result is not None
    return events, logic._last_result


def _params(
    operation: ImageOperation, inputs: list[Path], out: Path, **kw: object
) -> ImageParams:
    """Build ImageParams with the common fields filled in."""
    return ImageParams(
        operation=operation, input_paths=inputs, output_dir=out, **kw
    )  # type: ignore[arg-type]


# ─── Convert ──────────────────────────────────────────────────────────────────


class TestConvert:
    @pytest.mark.parametrize("fmt", list(OutputFormat))
    def test_writes_each_target_format(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path, fmt: OutputFormat
    ) -> None:
        source = make_image(tmp_path / "photo.png")
        _events, result = run(
            logic, _params(ImageOperation.CONVERT, [source], out_dir, output_format=fmt)
        )

        with Image.open(result.output_paths[0]) as converted:
            assert converted.format is not None
            assert converted.size == (200, 120)

    def test_transparency_is_flattened_for_jpeg(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """JPEG has no alpha channel; saving RGBA directly would raise."""
        source = make_image(tmp_path / "logo.png", mode="RGBA")
        _events, result = run(
            logic,
            _params(
                ImageOperation.CONVERT,
                [source],
                out_dir,
                output_format=OutputFormat.JPEG,
            ),
        )

        with Image.open(result.output_paths[0]) as converted:
            assert converted.mode == "RGB"

    def test_alpha_is_kept_for_png(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "logo.png", mode="RGBA")
        _events, result = run(
            logic,
            _params(
                ImageOperation.CONVERT, [source], out_dir, output_format=OutputFormat.PNG
            ),
        )

        with Image.open(result.output_paths[0]) as converted:
            assert converted.mode in {"RGBA", "LA"}

    def test_writes_into_the_module_subdirectory(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "photo.png")
        _events, result = run(logic, _params(ImageOperation.CONVERT, [source], out_dir))
        assert result.output_paths[0].parent.name == OUTPUT_SUBDIR

    def test_several_images_are_converted(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        sources = [make_image(tmp_path / f"img{i}.png") for i in range(3)]
        _events, result = run(logic, _params(ImageOperation.CONVERT, sources, out_dir))

        assert len(result.output_paths) == 3
        assert result.images_processed == 3


# ─── Resize ───────────────────────────────────────────────────────────────────


class TestResize:
    def test_caps_the_longest_edge(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "big.png", size=(800, 400))
        _events, result = run(
            logic, _params(ImageOperation.RESIZE, [source], out_dir, max_dimension=200)
        )

        with Image.open(result.output_paths[0]) as resized:
            assert max(resized.size) == 200

    def test_aspect_ratio_is_preserved(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "big.png", size=(800, 400))
        _events, result = run(
            logic, _params(ImageOperation.RESIZE, [source], out_dir, max_dimension=200)
        )

        with Image.open(result.output_paths[0]) as resized:
            assert resized.size == (200, 100)

    def test_a_portrait_image_caps_its_height(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "tall.png", size=(200, 800))
        _events, result = run(
            logic, _params(ImageOperation.RESIZE, [source], out_dir, max_dimension=400)
        )

        with Image.open(result.output_paths[0]) as resized:
            assert resized.size == (100, 400)

    def test_a_smaller_image_is_not_upscaled(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """Upscaling adds bytes without adding detail."""
        source = make_image(tmp_path / "small.png", size=(100, 50))
        _events, result = run(
            logic, _params(ImageOperation.RESIZE, [source], out_dir, max_dimension=1000)
        )

        with Image.open(result.output_paths[0]) as resized:
            assert resized.size == (100, 50)
        assert any("already" in note for note in result.warnings)


# ─── Compress to target ───────────────────────────────────────────────────────


class TestCompressToTarget:
    def test_lands_under_the_requested_size(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "photo.png", size=(1200, 900))
        _events, result = run(
            logic,
            _params(
                ImageOperation.COMPRESS_TO_TARGET,
                [source],
                out_dir,
                output_format=OutputFormat.JPEG,
                target_kb=40,
            ),
        )

        assert result.output_paths[0].stat().st_size <= 40 * BYTES_PER_KIB

    def test_the_result_is_a_readable_image(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "photo.png", size=(600, 400))
        _events, result = run(
            logic,
            _params(
                ImageOperation.COMPRESS_TO_TARGET,
                [source],
                out_dir,
                output_format=OutputFormat.JPEG,
                target_kb=30,
            ),
        )

        with Image.open(result.output_paths[0]) as compressed:
            assert compressed.size == (600, 400)

    def test_an_unreachable_target_warns_instead_of_failing(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """A 1 KB target is impossible for a large photo; say so, do not crash."""
        source = make_image(tmp_path / "photo.png", size=(1600, 1200))
        _events, result = run(
            logic,
            _params(
                ImageOperation.COMPRESS_TO_TARGET,
                [source],
                out_dir,
                output_format=OutputFormat.JPEG,
                target_kb=1,
            ),
        )

        assert result.output_paths[0].is_file()
        assert any("could not reach" in note for note in result.warnings)

    def test_a_generous_target_is_met_easily(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "photo.png", size=(200, 200))
        _events, result = run(
            logic,
            _params(
                ImageOperation.COMPRESS_TO_TARGET,
                [source],
                out_dir,
                output_format=OutputFormat.JPEG,
                target_kb=5000,
            ),
        )
        assert result.warnings == []


# ─── Metadata ─────────────────────────────────────────────────────────────────


class TestStripMetadata:
    def test_exif_is_removed(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = tmp_path / "tagged.jpg"
        image = Image.new("RGB", (60, 60), (10, 20, 30))
        exif = image.getexif()
        exif[0x010E] = "Sensitive location note"
        image.save(source, exif=exif)

        assert Image.open(source).getexif()

        _events, result = run(
            logic,
            _params(
                ImageOperation.STRIP_METADATA,
                [source],
                out_dir,
                output_format=OutputFormat.JPEG,
            ),
        )

        with Image.open(result.output_paths[0]) as cleaned:
            assert dict(cleaned.getexif()) == {}

    def test_pixels_are_preserved(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = tmp_path / "plain.png"
        Image.new("RGB", (20, 20), (10, 20, 30)).save(source)

        _events, result = run(
            logic,
            _params(
                ImageOperation.STRIP_METADATA,
                [source],
                out_dir,
                output_format=OutputFormat.PNG,
            ),
        )

        with Image.open(result.output_paths[0]) as cleaned:
            assert cleaned.getpixel((5, 5)) == (10, 20, 30)


# ─── SVG export ───────────────────────────────────────────────────────────────


class TestSvgToDensities:
    def test_writes_every_density_bucket(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_svg(tmp_path / "icon.svg")
        _events, result = run(
            logic, _params(ImageOperation.SVG_TO_DENSITIES, [source], out_dir)
        )
        assert len(result.output_paths) == len(ANDROID_DENSITIES)

    def test_each_bucket_is_scaled_correctly(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_svg(tmp_path / "icon.svg")
        _events, result = run(
            logic,
            _params(
                ImageOperation.SVG_TO_DENSITIES, [source], out_dir, base_size_dp=24
            ),
        )

        by_bucket = {p.parent.name: p for p in result.output_paths}
        for density, scale in ANDROID_DENSITIES.items():
            with Image.open(by_bucket[f"drawable-{density}"]) as png:
                assert png.size == (round(24 * scale), round(24 * scale))


class TestSvgToFavicons:
    def test_writes_every_standard_size(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_svg(tmp_path / "logo.svg")
        _events, result = run(
            logic, _params(ImageOperation.SVG_TO_FAVICONS, [source], out_dir)
        )

        folder = result.output_paths[0]
        produced = {p.name for p in folder.iterdir()}
        for size in FAVICON_SIZES:
            assert f"favicon-{size}x{size}.png" in produced

    def test_bundles_a_multi_resolution_ico(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_svg(tmp_path / "logo.svg")
        _events, result = run(
            logic, _params(ImageOperation.SVG_TO_FAVICONS, [source], out_dir)
        )

        ico = result.output_paths[0] / FAVICON_ICO_NAME
        assert ico.is_file()
        with Image.open(ico) as icon:
            assert icon.format == "ICO"


class TestSvgToVectorDrawable:
    def test_produces_android_vector_xml(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_svg(tmp_path / "icon.svg")
        _events, result = run(
            logic, _params(ImageOperation.SVG_TO_VECTOR_DRAWABLE, [source], out_dir)
        )

        xml = result.output_paths[0].read_text(encoding="utf-8")
        assert "<vector" in xml
        assert 'android:pathData="M4 4 H20 V20 H4 Z"' in xml

    def test_viewport_comes_from_the_view_box(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_svg(
            tmp_path / "icon.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 32">'
            '<path d="M0 0 H10 Z"/></svg>',
        )
        _events, result = run(
            logic, _params(ImageOperation.SVG_TO_VECTOR_DRAWABLE, [source], out_dir)
        )

        xml = result.output_paths[0].read_text(encoding="utf-8")
        assert 'android:viewportWidth="48.0"' in xml
        assert 'android:viewportHeight="32.0"' in xml

    def test_falls_back_to_width_and_height(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_svg(
            tmp_path / "icon.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" width="64px" height="64px">'
            '<path d="M0 0 H10 Z"/></svg>',
        )
        _events, result = run(
            logic, _params(ImageOperation.SVG_TO_VECTOR_DRAWABLE, [source], out_dir)
        )
        assert 'android:viewportWidth="64.0"' in result.output_paths[0].read_text(
            encoding="utf-8"
        )

    @pytest.mark.parametrize(
        ("fill", "expected"),
        [
            ("#FF5733", "#FFFF5733"),
            ("#f53", "#FFFF5533"),
            ("#80FF5733", "#80FF5733"),
            ("none", VECTOR_DEFAULT_FILL),
            ("currentColor", VECTOR_DEFAULT_FILL),
            ("red", VECTOR_DEFAULT_FILL),
            (None, VECTOR_DEFAULT_FILL),
            ("#ABCD", VECTOR_DEFAULT_FILL),
            ("#1234567890", VECTOR_DEFAULT_FILL),
        ],
    )
    def test_translates_fill_colours(
        self, logic: ImageSuiteLogic, fill: str | None, expected: str
    ) -> None:
        assert logic._android_colour(fill) == expected

    def test_unsupported_elements_are_reported(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """A silently dropped gradient would produce a wrong-looking icon."""
        source = make_svg(
            tmp_path / "icon.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            '<linearGradient id="g"/><path d="M0 0 H10 Z"/></svg>',
        )
        _events, result = run(
            logic, _params(ImageOperation.SVG_TO_VECTOR_DRAWABLE, [source], out_dir)
        )
        assert any("linearGradient" in note for note in result.warnings)

    def test_an_svg_without_paths_is_reported(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_svg(
            tmp_path / "icon.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            '<rect width="24" height="24"/></svg>',
        )
        _events, result = run(
            logic, _params(ImageOperation.SVG_TO_VECTOR_DRAWABLE, [source], out_dir)
        )
        assert any("no <path> elements" in note for note in result.warnings)

    @pytest.mark.parametrize(
        "view_box",
        ["not numbers here", "0 0 wide tall"],
        ids=["wrong-arity", "non-numeric"],
    )
    def test_a_malformed_view_box_falls_back(
        self, logic: ImageSuiteLogic, view_box: str
    ) -> None:
        root = ElementTree.fromstring(
            f'<svg viewBox="{view_box}" width="30" height="40"/>'
        )
        assert logic._svg_viewport(root) == (30.0, 40.0)


# ─── Shared behaviour ─────────────────────────────────────────────────────────


class TestExecutionContract:
    def test_every_operation_has_a_handler(self, logic: ImageSuiteLogic) -> None:
        assert set(logic._handlers()) == set(ImageOperation)

    def test_a_missing_input_is_reported_clearly(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        with pytest.raises(FileNotFoundError, match="File not found"):
            run(
                logic,
                _params(ImageOperation.CONVERT, [tmp_path / "absent.png"], out_dir),
            )

    def test_a_mismatched_extension_is_rejected(
        self, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "photo.png")
        with pytest.raises(ValueError, match="requires"):
            _params(ImageOperation.SVG_TO_FAVICONS, [source], out_dir)

    def test_progress_ends_at_one_hundred(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "photo.png")
        events, _result = run(logic, _params(ImageOperation.CONVERT, [source], out_dir))
        assert events[-1].percent == PROGRESS_COMPLETE

    def test_sizes_are_recorded(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_image(tmp_path / "photo.png")
        _events, result = run(logic, _params(ImageOperation.CONVERT, [source], out_dir))
        assert result.input_bytes > 0
        assert result.output_bytes > 0

    def test_directory_outputs_are_measured(
        self, logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_svg(tmp_path / "logo.svg")
        _events, result = run(
            logic, _params(ImageOperation.SVG_TO_FAVICONS, [source], out_dir)
        )
        assert result.output_bytes > 0

    def test_an_absent_output_measures_zero(
        self, logic: ImageSuiteLogic, tmp_path: Path
    ) -> None:
        assert logic._path_size(tmp_path / "nothing") == 0


class TestResultArithmetic:
    def _result(self, **kw: object) -> ImageResult:
        defaults: dict[str, object] = {
            "operation": ImageOperation.CONVERT,
            "output_paths": [Path("out.png")],
            "images_processed": 2,
            "input_bytes": 1000,
            "output_bytes": 250,
        }
        return ImageResult(**{**defaults, **kw})  # type: ignore[arg-type]

    def test_reports_bytes_saved(self) -> None:
        assert self._result().bytes_saved == 750

    def test_reports_the_percentage_saved(self) -> None:
        assert self._result().size_change_percent == pytest.approx(75.0)

    def test_growth_is_negative(self) -> None:
        assert self._result(output_bytes=1400).bytes_saved == -400

    def test_an_empty_input_does_not_divide_by_zero(self) -> None:
        assert self._result(input_bytes=0).size_change_percent == 0.0


# ─── EventBus wiring ──────────────────────────────────────────────────────────


@pytest.mark.asyncio()
async def test_register_and_unregister_round_trip(logic: ImageSuiteLogic) -> None:
    from core.event_bus import event_bus
    from modules.converters.image_suite.constants import EVENT_EXECUTE

    await logic.register()
    assert logic._on_execute in event_bus._subscribers[EVENT_EXECUTE]

    await logic.unregister()
    assert logic._on_execute not in event_bus._subscribers[EVENT_EXECUTE]


@pytest.mark.asyncio()
async def test_result_is_published_on_completion(
    logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
) -> None:
    from core.event_bus import event_bus
    from modules.converters.image_suite.constants import EVENT_DONE

    received: list[ImageResult] = []

    async def capture(payload: object) -> None:
        assert isinstance(payload, ImageResult)
        received.append(payload)

    event_bus.subscribe(EVENT_DONE, capture)
    try:
        source = make_image(tmp_path / "photo.png")
        await logic._on_execute(_params(ImageOperation.CONVERT, [source], out_dir))
    finally:
        event_bus.unsubscribe(EVENT_DONE, capture)

    assert len(received) == 1


@pytest.mark.asyncio()
async def test_a_failure_publishes_an_error_not_a_result(
    logic: ImageSuiteLogic, tmp_path: Path, out_dir: Path
) -> None:
    from core.event_bus import event_bus
    from modules.converters.image_suite.constants import EVENT_DONE, EVENT_ERROR

    done: list[object] = []
    errors: list[object] = []

    async def on_done(payload: object) -> None:
        done.append(payload)

    async def on_error(payload: object) -> None:
        errors.append(payload)

    event_bus.subscribe(EVENT_DONE, on_done)
    event_bus.subscribe(EVENT_ERROR, on_error)
    try:
        await logic._on_execute(
            _params(ImageOperation.CONVERT, [tmp_path / "absent.png"], out_dir)
        )
    finally:
        event_bus.unsubscribe(EVENT_DONE, on_done)
        event_bus.unsubscribe(EVENT_ERROR, on_error)

    assert done == []
    assert len(errors) == 1


@pytest.mark.asyncio()
async def test_a_non_params_payload_is_ignored(logic: ImageSuiteLogic) -> None:
    await logic._on_execute({"operation": "convert"})
    assert logic._last_result is None


# ─── HEIF registration ────────────────────────────────────────────────────────


class TestHeifRegistration:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "modules.converters.image_suite.logic._HEIF_REGISTERED", False
        )

    def test_registers_the_opener(self) -> None:
        assert register_heif() is True

    def test_registration_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-registering on every HEIC file would be pointless work."""
        register_heif()
        monkeypatch.setitem(sys.modules, "pillow_heif", None)
        assert register_heif() is True

    def test_a_missing_library_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def refuse(name: str, *args: object, **kwargs: object) -> object:
            if name == "pillow_heif":
                raise ImportError("no pillow_heif")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", refuse)
        assert register_heif() is False

    def test_a_heic_input_triggers_registration(
        self,
        logic: ImageSuiteLogic,
        tmp_path: Path,
        out_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[bool] = []
        monkeypatch.setattr(
            "modules.converters.image_suite.logic.register_heif",
            lambda: calls.append(True) or True,
        )
        # Pillow sniffs content rather than the extension, so a renamed PNG
        # still opens; what is under test is that the .heic suffix triggers
        # registration before the file is read.
        source = make_image(tmp_path / "photo.png")
        heic = tmp_path / "photo.heic"
        source.replace(heic)

        run(logic, _params(ImageOperation.CONVERT, [heic], out_dir))

        assert calls == [True]


# ─── Rasterisation failures ───────────────────────────────────────────────────


class TestRasteriseFailures:
    def test_an_unparsable_svg_is_reported(
        self, logic: ImageSuiteLogic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("svglib.svglib.svg2rlg", lambda _p: None)
        source = make_svg(tmp_path / "broken.svg")

        with pytest.raises(ValueError, match="could not be parsed"):
            logic._rasterise(source, tmp_path / "out.png", 32)

    def test_an_empty_drawing_is_reported(
        self, logic: ImageSuiteLogic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero-area page would divide by zero when computing the zoom."""

        class _Rect:
            width = 0.0
            height = 0.0

        class _Page:
            rect = _Rect()

        class _Doc:
            def __enter__(self) -> _Doc:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def __getitem__(self, _index: int) -> _Page:
                return _Page()

        monkeypatch.setattr("fitz.open", lambda **_kw: _Doc())
        source = make_svg(tmp_path / "empty.svg")

        with pytest.raises(ValueError, match="no drawable area"):
            logic._rasterise(source, tmp_path / "out.png", 32)

    def test_a_non_square_svg_is_letterboxed_not_stretched(
        self, logic: ImageSuiteLogic, tmp_path: Path
    ) -> None:
        source = make_svg(
            tmp_path / "wide.svg",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 12">'
            '<path d="M0 0 H48 V12 H0 Z" fill="#000000"/></svg>',
        )
        target = tmp_path / "out.png"
        logic._rasterise(source, target, 64)

        with Image.open(target) as png:
            assert png.size == (64, 64)


# ─── Helper branches ──────────────────────────────────────────────────────────


class TestHelperBranches:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("24", 24.0), ("24px", 24.0), ("", 24.0), (None, 24.0)]
    )
    def test_svg_lengths_are_parsed(
        self, logic: ImageSuiteLogic, raw: str | None, expected: float
    ) -> None:
        assert logic._svg_length(raw) == expected

    def test_an_unparsable_length_falls_back(self, logic: ImageSuiteLogic) -> None:
        assert logic._svg_length("auto") == 24.0

    def test_a_palette_image_gains_an_alpha_channel(
        self, logic: ImageSuiteLogic
    ) -> None:
        palette = Image.new("P", (10, 10))
        assert logic._prepare(palette, "PNG").mode == "RGBA"

    def test_a_palette_image_flattens_for_jpeg(self, logic: ImageSuiteLogic) -> None:
        palette = Image.new("P", (10, 10))
        assert logic._prepare(palette, "JPEG").mode == "RGB"

    def test_png_encoding_ignores_quality(self, logic: ImageSuiteLogic) -> None:
        """PNG is lossless, so passing quality would be meaningless."""
        image = Image.new("RGBA", (20, 20), (1, 2, 3, 255))
        assert logic._encode(image, "PNG", 10) == logic._encode(image, "PNG", 90)

    def test_jpeg_encoding_honours_quality(self, logic: ImageSuiteLogic) -> None:
        image = make_image(Path(tempfile.mkdtemp()) / "q.png", size=(200, 200))
        with Image.open(image) as opened:
            prepared = logic._prepare(opened, "JPEG")
            assert len(logic._encode(prepared, "JPEG", 10)) < len(
                logic._encode(prepared, "JPEG", 90)
            )
