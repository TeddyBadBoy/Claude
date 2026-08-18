from __future__ import annotations

import io
from pathlib import Path

import pytest

from drivezip.ranges import LocalFileRangeSource
from drivezip.reader import RangeReader

BLOCK = 4096


@pytest.fixture
def payload(tmp_path: Path) -> Path:
    path = tmp_path / "blob.bin"
    path.write_bytes(bytes(range(256)) * 400)  # 100 000 bytes
    return path


def test_reads_match_the_underlying_file(payload: Path) -> None:
    expected = payload.read_bytes()
    with LocalFileRangeSource(payload) as source:
        reader = RangeReader(source, block_size=BLOCK)
        assert reader.read() == expected


def test_seek_and_partial_reads(payload: Path) -> None:
    expected = payload.read_bytes()
    with LocalFileRangeSource(payload) as source:
        reader = RangeReader(source, block_size=BLOCK)

        reader.seek(10_000)
        assert reader.read(500) == expected[10_000:10_500]
        assert reader.tell() == 10_500

        reader.seek(-100, io.SEEK_END)
        assert reader.read() == expected[-100:]

        reader.seek(-50, io.SEEK_CUR)
        assert reader.read(50) == expected[-50:]

        reader.seek(len(expected) + 1000)
        assert reader.read(10) == b""


def test_negative_seek_is_rejected(payload: Path) -> None:
    with LocalFileRangeSource(payload) as source:
        reader = RangeReader(source, block_size=BLOCK)
        with pytest.raises(OSError):
            reader.seek(-1)


def test_repeated_reads_are_served_from_cache(payload: Path) -> None:
    with LocalFileRangeSource(payload) as source:
        reader = RangeReader(source, block_size=BLOCK, readahead_blocks=0)
        reader.seek(0)
        reader.read(100)
        after_first = reader.stats.requests

        for _ in range(20):
            reader.seek(0)
            reader.read(100)

        assert after_first == 1
        assert reader.stats.requests == 1
        assert reader.stats.cache_hits >= 20


def test_readahead_batches_sequential_reads(payload: Path) -> None:
    with LocalFileRangeSource(payload) as source:
        eager = RangeReader(source, block_size=BLOCK, readahead_blocks=7)
        eager.read(BLOCK * 8)
        assert eager.stats.requests == 1

    with LocalFileRangeSource(payload) as source:
        lazy = RangeReader(source, block_size=BLOCK, readahead_blocks=0)
        lazy.read(BLOCK * 8)
        assert lazy.stats.requests == 8


def test_prefetch_tail_costs_one_request(payload: Path) -> None:
    with LocalFileRangeSource(payload) as source:
        reader = RangeReader(source, block_size=BLOCK, readahead_blocks=0)
        reader.prefetch_tail(BLOCK * 5)
        assert reader.stats.requests == 1

        reader.seek(-BLOCK * 5, io.SEEK_END)
        reader.read(BLOCK * 5)
        assert reader.stats.requests == 1  # entirely served from the warmed cache


def test_cache_is_bounded(payload: Path) -> None:
    with LocalFileRangeSource(payload) as source:
        reader = RangeReader(source, block_size=BLOCK, max_cached_blocks=2, readahead_blocks=0)
        reader.read(len(payload.read_bytes()))
        assert len(reader._cache) <= 2  # noqa: SLF001 - the bound is the point of the test


def test_reader_rejects_nonsense_configuration(payload: Path) -> None:
    with LocalFileRangeSource(payload) as source:
        for kwargs in ({"block_size": 0}, {"max_cached_blocks": 0}, {"readahead_blocks": -1}):
            with pytest.raises(ValueError):
                RangeReader(source, **kwargs)


def test_empty_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    with LocalFileRangeSource(path) as source:
        reader = RangeReader(source, block_size=BLOCK)
        reader.prefetch_tail(1024)
        assert reader.read() == b""
        assert reader.stats.requests == 0
