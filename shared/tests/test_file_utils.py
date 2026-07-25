"""Unit tests for shared.file_utils."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from shared.constants import HASH_CHUNK_SIZE_BYTES
from shared.file_utils import (
    HASH_ALGORITHMS,
    directory_size,
    hash_file,
    safe_copy,
    safe_move,
    unique_destination,
)


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    """A small file with known contents."""
    path = tmp_path / "sample.txt"
    path.write_bytes(b"omniforge")
    return path


class TestHashFile:
    @pytest.mark.parametrize("algorithm", sorted(HASH_ALGORITHMS))
    def test_every_algorithm_returns_a_hex_digest(
        self, sample: Path, algorithm: str
    ) -> None:
        digest = hash_file(sample, algorithm)
        assert digest == digest.lower()
        int(digest, 16)

    def test_matches_hashlib_for_sha256(self, sample: Path) -> None:
        assert hash_file(sample, "sha256") == hashlib.sha256(b"omniforge").hexdigest()

    def test_defaults_to_xxh3_128(self, sample: Path) -> None:
        assert hash_file(sample) == hash_file(sample, "xxh3_128")

    def test_identical_contents_hash_identically(self, tmp_path: Path) -> None:
        first = tmp_path / "a.bin"
        second = tmp_path / "b.bin"
        first.write_bytes(b"same")
        second.write_bytes(b"same")
        assert hash_file(first) == hash_file(second)

    def test_differing_contents_hash_differently(self, tmp_path: Path) -> None:
        first = tmp_path / "a.bin"
        second = tmp_path / "b.bin"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        assert hash_file(first) != hash_file(second)

    def test_empty_file_hashes_without_error(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.bin"
        empty.touch()
        assert hash_file(empty)

    def test_file_larger_than_one_chunk_is_streamed_correctly(self, tmp_path: Path) -> None:
        """Verifies the chunked read loop, not just the single-chunk path."""
        payload = b"x" * (HASH_CHUNK_SIZE_BYTES + 1024)
        big = tmp_path / "big.bin"
        big.write_bytes(payload)
        assert hash_file(big, "sha256") == hashlib.sha256(payload).hexdigest()

    def test_unknown_algorithm_raises_value_error(self, sample: Path) -> None:
        with pytest.raises(ValueError, match="Unsupported hash algorithm"):
            hash_file(sample, "sha3_512")

    def test_missing_file_raises_os_error(self, tmp_path: Path) -> None:
        with pytest.raises(OSError):
            hash_file(tmp_path / "absent.bin")


class TestDirectorySize:
    def test_absent_path_measures_zero(self, tmp_path: Path) -> None:
        assert directory_size(tmp_path / "nope") == (0, 0)

    def test_empty_directory_measures_zero(self, tmp_path: Path) -> None:
        assert directory_size(tmp_path) == (0, 0)

    def test_a_file_path_measures_just_that_file(self, sample: Path) -> None:
        assert directory_size(sample) == (9, 1)

    def test_sums_files_across_nested_directories(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "top.bin").write_bytes(b"1234")
        (tmp_path / "a" / "mid.bin").write_bytes(b"12345")
        (tmp_path / "a" / "b" / "deep.bin").write_bytes(b"123")

        assert directory_size(tmp_path) == (12, 3)

    def test_empty_subdirectories_do_not_count_as_files(self, tmp_path: Path) -> None:
        (tmp_path / "empty_dir").mkdir()
        (tmp_path / "one.bin").write_bytes(b"12")
        assert directory_size(tmp_path) == (2, 1)


class TestUniqueDestination:
    def test_free_path_is_returned_unchanged(self, tmp_path: Path) -> None:
        target = tmp_path / "free.txt"
        assert unique_destination(target) == target

    def test_first_collision_gets_suffix_one(self, tmp_path: Path) -> None:
        (tmp_path / "taken.txt").touch()
        assert unique_destination(tmp_path / "taken.txt").name == "taken (1).txt"

    def test_suffix_increments_past_existing_variants(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").touch()
        (tmp_path / "f (1).txt").touch()
        (tmp_path / "f (2).txt").touch()
        assert unique_destination(tmp_path / "f.txt").name == "f (3).txt"

    def test_extensionless_names_are_handled(self, tmp_path: Path) -> None:
        (tmp_path / "LICENSE").touch()
        assert unique_destination(tmp_path / "LICENSE").name == "LICENSE (1)"


class TestSafeCopy:
    def test_copies_contents_and_leaves_the_source(self, sample: Path, tmp_path: Path) -> None:
        result = safe_copy(sample, tmp_path / "out" / "copy.txt")
        assert result.read_bytes() == b"omniforge"
        assert sample.exists()

    def test_creates_missing_parent_directories(self, sample: Path, tmp_path: Path) -> None:
        result = safe_copy(sample, tmp_path / "x" / "y" / "z" / "copy.txt")
        assert result.exists()

    def test_never_overwrites_an_existing_file(self, sample: Path, tmp_path: Path) -> None:
        existing = tmp_path / "dest.txt"
        existing.write_bytes(b"original")

        result = safe_copy(sample, existing)

        assert result != existing
        assert existing.read_bytes() == b"original"
        assert result.read_bytes() == b"omniforge"


class TestSafeMove:
    def test_moves_contents_and_removes_the_source(self, sample: Path, tmp_path: Path) -> None:
        result = safe_move(sample, tmp_path / "moved" / "sample.txt")
        assert result.read_bytes() == b"omniforge"
        assert not sample.exists()

    def test_never_overwrites_an_existing_file(self, sample: Path, tmp_path: Path) -> None:
        existing = tmp_path / "dest.txt"
        existing.write_bytes(b"original")

        result = safe_move(sample, existing)

        assert existing.read_bytes() == b"original"
        assert result.read_bytes() == b"omniforge"

    def test_moves_a_whole_directory(self, tmp_path: Path) -> None:
        source = tmp_path / "tree"
        (source / "inner").mkdir(parents=True)
        (source / "inner" / "file.txt").write_bytes(b"data")

        result = safe_move(source, tmp_path / "relocated")

        assert (result / "inner" / "file.txt").read_bytes() == b"data"
        assert not source.exists()
