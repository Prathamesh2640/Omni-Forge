"""Unit tests for the pdf_suite logic layer.

These build genuine PDFs with PyMuPDF rather than mocking it, so each
operation is verified against real document structure.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import fitz
import pytest

from core.models import ProgressEvent
from modules.converters.pdf_suite.constants import (
    OUTPUT_SUBDIR,
    PROGRESS_COMPLETE,
)
from modules.converters.pdf_suite.logic import PdfPasswordError, PdfSuiteLogic
from modules.converters.pdf_suite.models import (
    CompressPreset,
    PdfMetadata,
    PdfOperation,
    PdfParams,
    PdfResult,
    SplitMode,
)


@pytest.fixture()
def logic() -> PdfSuiteLogic:
    """Fresh logic instance with no EventBus registrations."""
    return PdfSuiteLogic()


def make_pdf(path: Path, pages: int = 3, text: str = "OmniForge") -> Path:
    """Write a simple multi-page PDF and return its path.

    Args:
        path: Destination file.
        pages: Number of pages to create.
        text: Text drawn on every page.
    """
    document = fitz.open()
    try:
        for number in range(pages):
            page = document.new_page()
            page.insert_text((72, 72), f"{text} page {number + 1}")
        document.save(path)
    finally:
        document.close()
    return path


def make_encrypted_pdf(path: Path, password: str, pages: int = 2) -> Path:
    """Write a password-protected PDF and return its path.

    Args:
        path: Destination file.
        password: User password to apply.
        pages: Number of pages to create.
    """
    document = fitz.open()
    try:
        for _ in range(pages):
            document.new_page().insert_text((72, 72), "secret")
        document.save(
            path,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            user_pw=password,
            owner_pw=password,
        )
    finally:
        document.close()
    return path


def run(logic: PdfSuiteLogic, params: PdfParams) -> tuple[list[ProgressEvent], PdfResult]:
    """Execute an operation to completion.

    Args:
        logic: The logic instance.
        params: Operation parameters.

    Returns:
        The emitted events and the recorded result.
    """

    async def drive() -> list[ProgressEvent]:
        return [event async for event in logic.execute(params)]

    events = asyncio.run(drive())
    assert logic._last_result is not None
    return events, logic._last_result


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    """Directory operations write into."""
    return tmp_path / "exports"


def _params(operation: PdfOperation, inputs: list[Path], out: Path, **kw: object) -> PdfParams:
    """Build PdfParams with the common fields filled in."""
    return PdfParams(operation=operation, input_paths=inputs, output_dir=out, **kw)  # type: ignore[arg-type]


# ─── Merge ────────────────────────────────────────────────────────────────────


class TestMerge:
    @pytest.fixture()
    def sources(self, tmp_path: Path) -> list[Path]:
        return [
            make_pdf(tmp_path / "a.pdf", pages=2),
            make_pdf(tmp_path / "b.pdf", pages=3),
        ]

    def test_page_counts_are_summed(
        self, logic: PdfSuiteLogic, sources: list[Path], out_dir: Path
    ) -> None:
        _events, result = run(logic, _params(PdfOperation.MERGE, sources, out_dir))

        with fitz.open(result.output_paths[0]) as merged:
            assert merged.page_count == 5

    def test_produces_a_single_file(
        self, logic: PdfSuiteLogic, sources: list[Path], out_dir: Path
    ) -> None:
        _events, result = run(logic, _params(PdfOperation.MERGE, sources, out_dir))
        assert len(result.output_paths) == 1
        assert result.output_paths[0].is_file()

    def test_input_order_is_preserved(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        first = make_pdf(tmp_path / "first.pdf", pages=1, text="ALPHA")
        second = make_pdf(tmp_path / "second.pdf", pages=1, text="BETA")

        _events, result = run(
            logic, _params(PdfOperation.MERGE, [second, first], out_dir)
        )

        with fitz.open(result.output_paths[0]) as merged:
            assert "BETA" in merged[0].get_text()
            assert "ALPHA" in merged[1].get_text()

    def test_writes_into_the_module_subdirectory(
        self, logic: PdfSuiteLogic, sources: list[Path], out_dir: Path
    ) -> None:
        _events, result = run(logic, _params(PdfOperation.MERGE, sources, out_dir))
        assert result.output_paths[0].parent.name == OUTPUT_SUBDIR

    def test_merging_needs_two_files(self, tmp_path: Path, out_dir: Path) -> None:
        with pytest.raises(ValueError, match="at least two"):
            _params(PdfOperation.MERGE, [make_pdf(tmp_path / "only.pdf")], out_dir)


# ─── Split ────────────────────────────────────────────────────────────────────


class TestSplit:
    @pytest.fixture()
    def source(self, tmp_path: Path) -> Path:
        return make_pdf(tmp_path / "long.pdf", pages=7)

    def test_every_n_produces_ceil_chunks(
        self, logic: PdfSuiteLogic, source: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic, _params(PdfOperation.SPLIT, [source], out_dir, every_n=3)
        )
        assert len(result.output_paths) == 3

    def test_every_n_final_chunk_holds_the_remainder(
        self, logic: PdfSuiteLogic, source: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic, _params(PdfOperation.SPLIT, [source], out_dir, every_n=3)
        )
        with fitz.open(result.output_paths[-1]) as tail:
            assert tail.page_count == 1

    def test_single_pages_yields_one_file_per_page(
        self, logic: PdfSuiteLogic, source: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(
                PdfOperation.SPLIT, [source], out_dir, split_mode=SplitMode.SINGLE_PAGES
            ),
        )
        assert len(result.output_paths) == 7
        with fitz.open(result.output_paths[0]) as part:
            assert part.page_count == 1

    def test_page_range_extracts_just_that_span(
        self, logic: PdfSuiteLogic, source: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(
                PdfOperation.SPLIT,
                [source],
                out_dir,
                split_mode=SplitMode.PAGE_RANGE,
                page_range=(2, 4),
            ),
        )
        with fitz.open(result.output_paths[0]) as part:
            assert part.page_count == 3

    def test_a_range_past_the_end_is_clamped(
        self, logic: PdfSuiteLogic, source: Path, out_dir: Path
    ) -> None:
        _events, result = run(
            logic,
            _params(
                PdfOperation.SPLIT,
                [source],
                out_dir,
                split_mode=SplitMode.PAGE_RANGE,
                page_range=(6, 99),
            ),
        )
        with fitz.open(result.output_paths[0]) as part:
            assert part.page_count == 2

    def test_a_range_beyond_the_document_is_rejected(
        self, logic: PdfSuiteLogic, source: Path, out_dir: Path
    ) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            run(
                logic,
                _params(
                    PdfOperation.SPLIT,
                    [source],
                    out_dir,
                    split_mode=SplitMode.PAGE_RANGE,
                    page_range=(50, 60),
                ),
            )

    def test_an_inverted_range_is_rejected(self, tmp_path: Path, out_dir: Path) -> None:
        with pytest.raises(ValueError, match="not be before"):
            _params(
                PdfOperation.SPLIT,
                [make_pdf(tmp_path / "s.pdf")],
                out_dir,
                split_mode=SplitMode.PAGE_RANGE,
                page_range=(5, 2),
            )

    def test_a_missing_range_is_rejected(self, tmp_path: Path, out_dir: Path) -> None:
        with pytest.raises(ValueError, match="needs a first and last page"):
            _params(
                PdfOperation.SPLIT,
                [make_pdf(tmp_path / "s.pdf")],
                out_dir,
                split_mode=SplitMode.PAGE_RANGE,
                page_range=None,
            )

    @pytest.mark.parametrize("first", [0, -3])
    def test_a_non_positive_first_page_is_rejected(
        self, tmp_path: Path, out_dir: Path, first: int
    ) -> None:
        """PDF pages are 1-based in the UI; page 0 does not exist."""
        with pytest.raises(ValueError, match="must be 1 or greater"):
            _params(
                PdfOperation.SPLIT,
                [make_pdf(tmp_path / "s.pdf")],
                out_dir,
                split_mode=SplitMode.PAGE_RANGE,
                page_range=(first, 5),
            )


# ─── Compress ─────────────────────────────────────────────────────────────────


class TestCompress:
    def test_produces_a_readable_document(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf", pages=4)
        _events, result = run(
            logic, _params(PdfOperation.COMPRESS, [source], out_dir)
        )

        with fitz.open(result.output_paths[0]) as compressed:
            assert compressed.page_count == 4

    def test_text_survives_compression(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """Only rasters are resampled; the text layer must be untouched."""
        source = make_pdf(tmp_path / "doc.pdf", pages=1, text="KEEPME")
        _events, result = run(
            logic,
            _params(PdfOperation.COMPRESS, [source], out_dir, preset=CompressPreset.SCREEN),
        )

        with fitz.open(result.output_paths[0]) as compressed:
            assert "KEEPME" in compressed[0].get_text()

    @pytest.mark.parametrize("preset", list(CompressPreset))
    def test_every_preset_runs(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path, preset: CompressPreset
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf", pages=2)
        _events, result = run(
            logic, _params(PdfOperation.COMPRESS, [source], out_dir, preset=preset)
        )
        assert result.output_paths[0].is_file()

    def test_reports_the_size_change(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf", pages=3)
        _events, result = run(logic, _params(PdfOperation.COMPRESS, [source], out_dir))

        assert result.input_bytes > 0
        assert result.output_bytes > 0
        assert result.bytes_saved == result.input_bytes - result.output_bytes

    def test_compress_preserves_page_content(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """Regression: resampling must not blank the page.

        ``update_stream`` swapped only the image bytes and left the old
        ``/Filter``/``/Width``/``/Height``, so the page rendered blank and the
        embedded image was unreadable. ``replace_image`` rewrites the object
        metadata too. This asserts the page still renders with real colour and
        the embedded raster is smaller than the original.
        """
        source = make_pdf_with_image(tmp_path / "photo.pdf", size=800, dpi=300)
        _events, result = run(
            logic,
            _params(PdfOperation.COMPRESS, [source], out_dir, preset=CompressPreset.SCREEN),
        )

        import io

        from PIL import Image

        with fitz.open(result.output_paths[0]) as compressed:
            page = compressed[0]

            # The embedded image must still be a valid, decodable object — the
            # broken update_stream left a mismatched /Filter that made this fail.
            images = page.get_images(full=True)
            assert images, "the raster image was dropped entirely"
            extracted = compressed.extract_image(images[0][0])
            Image.open(io.BytesIO(extracted["image"])).verify()

            # The source is a solid red square. The broken path rendered the
            # region *black*; a generic "non-white" check would pass on that, so
            # assert the source colour itself survives the round-trip.
            pixmap = page.get_pixmap(dpi=72)
            samples, stride = pixmap.samples, pixmap.n
            reddish = sum(
                1
                for offset in range(0, len(samples), stride)
                if samples[offset] > 150
                and samples[offset + 1] < 120
                and samples[offset + 2] < 120
            )
            assert reddish > 0, "the compressed page lost the image (rendered blank/black)"

        # And it should actually be smaller (the resample did something).
        assert result.output_bytes < result.input_bytes


# ─── Password ─────────────────────────────────────────────────────────────────


class TestRemovePassword:
    def test_unlocks_with_the_correct_password(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_encrypted_pdf(tmp_path / "locked.pdf", "hunter2")

        _events, result = run(
            logic,
            _params(PdfOperation.REMOVE_PASSWORD, [source], out_dir, password="hunter2"),
        )

        with fitz.open(result.output_paths[0]) as unlocked:
            # PyMuPDF reports this as an int, not a bool.
            assert not unlocked.needs_pass
            assert unlocked.page_count == 2

    def test_a_wrong_password_is_rejected(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_encrypted_pdf(tmp_path / "locked.pdf", "hunter2")

        with pytest.raises(PdfPasswordError, match="password-protected"):
            run(
                logic,
                _params(
                    PdfOperation.REMOVE_PASSWORD, [source], out_dir, password="wrong"
                ),
            )

    def test_a_missing_password_is_rejected(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_encrypted_pdf(tmp_path / "locked.pdf", "hunter2")

        with pytest.raises(PdfPasswordError):
            run(logic, _params(PdfOperation.REMOVE_PASSWORD, [source], out_dir))

    def test_other_operations_accept_an_encrypted_source(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """The password applies to every operation, not just unlocking."""
        source = make_encrypted_pdf(tmp_path / "locked.pdf", "pw", pages=4)

        _events, result = run(
            logic,
            _params(PdfOperation.SPLIT, [source], out_dir, every_n=2, password="pw"),
        )

        assert len(result.output_paths) == 2


# ─── Metadata ─────────────────────────────────────────────────────────────────


class TestEditMetadata:
    def test_writes_the_supplied_fields(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf")
        _events, result = run(
            logic,
            _params(
                PdfOperation.EDIT_METADATA,
                [source],
                out_dir,
                metadata=PdfMetadata(title="Quarterly Report", author="Prathamesh"),
            ),
        )

        with fitz.open(result.output_paths[0]) as updated:
            assert updated.metadata is not None
            assert updated.metadata["title"] == "Quarterly Report"
            assert updated.metadata["author"] == "Prathamesh"

    def test_untouched_fields_are_preserved(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """Editing the title must not blank the author."""
        source = tmp_path / "doc.pdf"
        document = fitz.open()
        document.new_page()
        document.set_metadata({"author": "Original Author"})
        document.save(source)
        document.close()

        _events, result = run(
            logic,
            _params(
                PdfOperation.EDIT_METADATA,
                [source],
                out_dir,
                metadata=PdfMetadata(title="New Title"),
            ),
        )

        with fitz.open(result.output_paths[0]) as updated:
            assert updated.metadata is not None
            assert updated.metadata["author"] == "Original Author"

    def test_an_empty_edit_is_rejected(self, tmp_path: Path, out_dir: Path) -> None:
        with pytest.raises(ValueError, match="at least one metadata field"):
            _params(
                PdfOperation.EDIT_METADATA, [make_pdf(tmp_path / "d.pdf")], out_dir
            )


# ─── Rotate ───────────────────────────────────────────────────────────────────


class TestRotate:
    @pytest.mark.parametrize("angle", [90, 180, 270])
    def test_applies_the_requested_rotation(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path, angle: int
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf", pages=2)
        _events, result = run(
            logic, _params(PdfOperation.ROTATE, [source], out_dir, rotation=angle)
        )

        with fitz.open(result.output_paths[0]) as rotated:
            assert rotated[0].rotation == angle

    def test_rotation_accumulates_on_an_already_rotated_page(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = tmp_path / "doc.pdf"
        document = fitz.open()
        page = document.new_page()
        page.set_rotation(90)
        document.save(source)
        document.close()

        _events, result = run(
            logic, _params(PdfOperation.ROTATE, [source], out_dir, rotation=270)
        )

        with fitz.open(result.output_paths[0]) as rotated:
            assert rotated[0].rotation == 0

    def test_an_invalid_angle_is_rejected(self, tmp_path: Path, out_dir: Path) -> None:
        with pytest.raises(ValueError, match="Rotation must be"):
            _params(
                PdfOperation.ROTATE, [make_pdf(tmp_path / "d.pdf")], out_dir, rotation=45
            )


# ─── Extraction ───────────────────────────────────────────────────────────────


class TestExtractText:
    def test_writes_the_text_layer(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf", pages=2, text="FINDME")
        _events, result = run(
            logic, _params(PdfOperation.EXTRACT_TEXT, [source], out_dir)
        )

        content = result.output_paths[0].read_text(encoding="utf-8")
        assert content.count("FINDME") == 2

    def test_output_is_a_text_file(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf")
        _events, result = run(
            logic, _params(PdfOperation.EXTRACT_TEXT, [source], out_dir)
        )
        assert result.output_paths[0].suffix == ".txt"


class TestExtractImages:
    def test_creates_the_output_directory(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf")
        _events, result = run(
            logic, _params(PdfOperation.EXTRACT_IMAGES, [source], out_dir)
        )
        assert result.output_paths[0].is_dir()

    def test_a_document_without_images_yields_an_empty_folder(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "text_only.pdf")
        _events, result = run(
            logic, _params(PdfOperation.EXTRACT_IMAGES, [source], out_dir)
        )
        assert list(result.output_paths[0].iterdir()) == []


# ─── Shared behaviour ─────────────────────────────────────────────────────────


class TestExecutionContract:
    def test_a_missing_input_is_reported_clearly(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        with pytest.raises(FileNotFoundError, match="File not found"):
            run(
                logic,
                _params(PdfOperation.EXTRACT_TEXT, [tmp_path / "absent.pdf"], out_dir),
            )

    def test_progress_ends_at_one_hundred(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf", pages=4)
        events, _result = run(logic, _params(PdfOperation.ROTATE, [source], out_dir))

        assert events[-1].percent == PROGRESS_COMPLETE

    def test_progress_never_decreases(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf", pages=6)
        events, _result = run(
            logic, _params(PdfOperation.SPLIT, [source], out_dir, every_n=2)
        )

        percents = [e.percent for e in events]
        assert percents == sorted(percents)

    def test_enough_checkpoints_are_emitted(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """Rule D-08 — long operations report progress, not a frozen bar."""
        source = make_pdf(tmp_path / "doc.pdf", pages=12)
        events, _result = run(
            logic,
            _params(
                PdfOperation.SPLIT, [source], out_dir, split_mode=SplitMode.SINGLE_PAGES
            ),
        )
        assert len(events) >= 10

    def test_the_final_event_carries_the_output_path(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "doc.pdf")
        events, _result = run(
            logic, _params(PdfOperation.EXTRACT_TEXT, [source], out_dir)
        )
        assert events[-1].output_path is not None

    @pytest.mark.parametrize(
        "operation",
        [
            PdfOperation.SPLIT,
            PdfOperation.COMPRESS,
            PdfOperation.ROTATE,
            PdfOperation.EXTRACT_TEXT,
        ],
    )
    def test_single_file_operations_reject_extra_inputs(
        self, tmp_path: Path, out_dir: Path, operation: PdfOperation
    ) -> None:
        files = [make_pdf(tmp_path / "a.pdf"), make_pdf(tmp_path / "b.pdf")]
        with pytest.raises(ValueError, match="exactly one"):
            _params(operation, files, out_dir)

    def test_every_operation_has_a_handler(self, logic: PdfSuiteLogic) -> None:
        """A new operation without an implementation would KeyError at runtime."""
        assert set(logic._handlers()) == set(PdfOperation)


class TestResultArithmetic:
    def _result(self, **kw: object) -> PdfResult:
        defaults: dict[str, object] = {
            "operation": PdfOperation.COMPRESS,
            "output_paths": [Path("out.pdf")],
            "pages_processed": 3,
            "input_bytes": 1000,
            "output_bytes": 400,
        }
        return PdfResult(**{**defaults, **kw})  # type: ignore[arg-type]

    def test_reports_bytes_saved(self) -> None:
        assert self._result().bytes_saved == 600

    def test_reports_the_percentage_saved(self) -> None:
        assert self._result().size_change_percent == pytest.approx(60.0)

    def test_growth_is_reported_as_negative(self) -> None:
        assert self._result(output_bytes=1500).bytes_saved == -500

    def test_an_empty_input_does_not_divide_by_zero(self) -> None:
        assert self._result(input_bytes=0, output_bytes=0).size_change_percent == 0.0


# ─── EventBus wiring ──────────────────────────────────────────────────────────


@pytest.mark.asyncio()
async def test_register_and_unregister_round_trip(logic: PdfSuiteLogic) -> None:
    from core.event_bus import event_bus
    from modules.converters.pdf_suite.constants import EVENT_EXECUTE

    await logic.register()
    assert logic._on_execute in event_bus._subscribers[EVENT_EXECUTE]

    await logic.unregister()
    assert logic._on_execute not in event_bus._subscribers[EVENT_EXECUTE]


@pytest.mark.asyncio()
async def test_result_is_published_on_completion(
    logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
) -> None:
    from core.event_bus import event_bus
    from modules.converters.pdf_suite.constants import EVENT_DONE

    received: list[PdfResult] = []

    async def capture(payload: object) -> None:
        assert isinstance(payload, PdfResult)
        received.append(payload)

    event_bus.subscribe(EVENT_DONE, capture)
    try:
        source = make_pdf(tmp_path / "doc.pdf", pages=2)
        await logic._on_execute(
            _params(PdfOperation.EXTRACT_TEXT, [source], out_dir)
        )
    finally:
        event_bus.unsubscribe(EVENT_DONE, capture)

    assert len(received) == 1
    assert received[0].pages_processed == 2


@pytest.mark.asyncio()
async def test_a_failure_publishes_an_error_not_a_result(
    logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
) -> None:
    from core.event_bus import event_bus
    from modules.converters.pdf_suite.constants import EVENT_DONE, EVENT_ERROR

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
            _params(PdfOperation.EXTRACT_TEXT, [tmp_path / "absent.pdf"], out_dir)
        )
    finally:
        event_bus.unsubscribe(EVENT_DONE, on_done)
        event_bus.unsubscribe(EVENT_ERROR, on_error)

    assert done == []
    assert len(errors) == 1


@pytest.mark.asyncio()
async def test_a_non_params_payload_is_ignored(logic: PdfSuiteLogic) -> None:
    await logic._on_execute({"operation": "merge"})
    assert logic._last_result is None


# ─── Documents containing real raster images ──────────────────────────────────


def make_pdf_with_image(path: Path, size: int = 400, dpi: int = 300) -> Path:
    """Write a PDF holding one embedded raster image.

    Args:
        path: Destination file.
        size: Pixel width and height of the image.
        dpi: Resolution recorded on the image, which decides whether the
            compressor considers it worth resampling.
    """
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, size, size))
    pixmap.set_rect(pixmap.irect, (200, 40, 40))
    pixmap.set_dpi(dpi, dpi)

    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_image(fitz.Rect(0, 0, size / 2, size / 2), pixmap=pixmap)
        document.save(path)
    finally:
        document.close()
    return path


class TestImageHandling:
    def test_extracts_an_embedded_image(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf_with_image(tmp_path / "photo.pdf")
        _events, result = run(
            logic, _params(PdfOperation.EXTRACT_IMAGES, [source], out_dir)
        )

        saved = list(result.output_paths[0].iterdir())
        assert len(saved) == 1
        assert saved[0].stat().st_size > 0

    def test_extracted_filename_records_the_page(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf_with_image(tmp_path / "photo.pdf")
        _events, result = run(
            logic, _params(PdfOperation.EXTRACT_IMAGES, [source], out_dir)
        )
        saved = next(iter(result.output_paths[0].iterdir()))
        assert saved.name.startswith("page001_img01")

    def test_a_directory_output_is_measured(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """Output size must account for folders, not just single files."""
        source = make_pdf_with_image(tmp_path / "photo.pdf")
        _events, result = run(
            logic, _params(PdfOperation.EXTRACT_IMAGES, [source], out_dir)
        )
        assert result.output_bytes > 0

    def test_a_high_resolution_image_is_resampled(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf_with_image(tmp_path / "photo.pdf", size=600, dpi=300)
        events, result = run(
            logic,
            _params(
                PdfOperation.COMPRESS, [source], out_dir, preset=CompressPreset.SCREEN
            ),
        )

        assert result.output_paths[0].is_file()
        assert any("images" in event.message for event in events)

    def test_a_low_resolution_image_is_left_alone(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """Upscaling a small image would cost size for no quality gain."""
        source = make_pdf_with_image(tmp_path / "photo.pdf", size=400, dpi=72)
        events, _result = run(
            logic,
            _params(
                PdfOperation.COMPRESS, [source], out_dir, preset=CompressPreset.PRINT
            ),
        )
        assert any("Recompressed 0 images" in event.message for event in events)

    def test_a_tiny_image_is_below_the_resample_threshold(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf_with_image(tmp_path / "icon.pdf", size=40, dpi=300)
        events, _result = run(
            logic,
            _params(
                PdfOperation.COMPRESS, [source], out_dir, preset=CompressPreset.SCREEN
            ),
        )
        assert any("Recompressed 0 images" in event.message for event in events)

    def test_an_unreadable_image_does_not_abort_compression(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A corrupt image must be skipped, not fail the whole document."""
        source = make_pdf_with_image(tmp_path / "photo.pdf", size=600, dpi=300)

        def refuse(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("corrupt image stream")

        monkeypatch.setattr(fitz, "Pixmap", refuse)

        _events, result = run(
            logic, _params(PdfOperation.COMPRESS, [source], out_dir)
        )
        assert result.output_paths[0].is_file()


# ─── DOCX conversion ──────────────────────────────────────────────────────────


class TestToDocx:
    def test_produces_a_docx_file(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "report.pdf", pages=1, text="CONVERTME")
        _events, result = run(logic, _params(PdfOperation.TO_DOCX, [source], out_dir))

        target = result.output_paths[0]
        assert target.suffix == ".docx"
        assert target.stat().st_size > 0

    def test_the_docx_is_a_valid_zip_container(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """A DOCX is an OPC package; a truncated write would not open."""
        import zipfile

        source = make_pdf(tmp_path / "report.pdf", pages=1)
        _events, result = run(logic, _params(PdfOperation.TO_DOCX, [source], out_dir))

        with zipfile.ZipFile(result.output_paths[0]) as archive:
            assert "word/document.xml" in archive.namelist()


# ─── Helper edge cases ────────────────────────────────────────────────────────


class TestHelpers:
    def test_scaling_an_empty_operation_reports_the_band_end(
        self, logic: PdfSuiteLogic
    ) -> None:
        from modules.converters.pdf_suite.constants import PROGRESS_WORK_END

        assert logic._scaled(0, 0) == PROGRESS_WORK_END

    def test_measuring_an_absent_path_returns_zero(
        self, logic: PdfSuiteLogic, tmp_path: Path
    ) -> None:
        assert logic._path_size(tmp_path / "nothing-here") == 0


class TestToDocxPassword:
    """TO_DOCX must honour the password every other operation accepts.

    pdf2docx cannot open an encrypted PDF, so the source is decrypted into a
    scratch copy first. Previously the password was simply dropped and the
    conversion failed on documents the rest of the suite handled.
    """

    def test_converts_an_encrypted_source(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_encrypted_pdf(tmp_path / "locked.pdf", "hunter2", pages=1)

        _events, result = run(
            logic,
            _params(PdfOperation.TO_DOCX, [source], out_dir, password="hunter2"),
        )

        assert result.output_paths[0].is_file()
        assert result.output_paths[0].stat().st_size > 0

    def test_the_scratch_copy_is_cleaned_up(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        """A decrypted copy must never be left lying on disk."""
        source = make_encrypted_pdf(tmp_path / "locked.pdf", "hunter2", pages=1)

        _events, result = run(
            logic,
            _params(PdfOperation.TO_DOCX, [source], out_dir, password="hunter2"),
        )

        leftovers = list(result.output_paths[0].parent.glob("*_decrypted_tmp.pdf"))
        assert leftovers == []

    def test_a_wrong_password_is_reported(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_encrypted_pdf(tmp_path / "locked.pdf", "hunter2", pages=1)

        with pytest.raises(PdfPasswordError):
            run(
                logic,
                _params(PdfOperation.TO_DOCX, [source], out_dir, password="wrong"),
            )


class TestOutputFileCount:
    """The count is taken in the worker thread, not while painting the card.

    The UI used to rglob() the output folder during render — disk I/O on the
    event loop, right when the result appears.
    """

    def test_a_single_file_output_counts_one(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        sources = [
            make_pdf(tmp_path / "a.pdf", pages=1),
            make_pdf(tmp_path / "b.pdf", pages=1),
        ]
        _events, result = run(logic, _params(PdfOperation.MERGE, sources, out_dir))
        assert result.output_file_count == 1

    def test_a_folder_output_counts_its_contents(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf_with_image(tmp_path / "photo.pdf")
        _events, result = run(
            logic, _params(PdfOperation.EXTRACT_IMAGES, [source], out_dir)
        )

        produced = list(result.output_paths[0].rglob("*"))
        assert result.output_file_count == len([p for p in produced if p.is_file()])
        assert result.output_file_count >= 1

    def test_split_counts_every_part(
        self, logic: PdfSuiteLogic, tmp_path: Path, out_dir: Path
    ) -> None:
        source = make_pdf(tmp_path / "long.pdf", pages=6)
        _events, result = run(
            logic, _params(PdfOperation.SPLIT, [source], out_dir, every_n=2)
        )
        assert result.output_file_count == 3
