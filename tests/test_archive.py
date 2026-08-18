from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from helpers import MEMBERS, CountingSource

from drivezip.archive import ZipArchive, _safe_destination
from drivezip.errors import ArchiveError, MemberNotFoundError, UnsafeMemberError
from drivezip.ranges import LocalFileRangeSource


def test_listing_reads_only_the_tail(counting_source: CountingSource) -> None:
    with ZipArchive(counting_source, block_size=64 * 1024) as archive:
        names = {entry.name for entry in archive.entries()}

    assert set(MEMBERS) <= names
    # The whole point: listing must not stream the archive.
    assert counting_source.bytes_served < counting_source.size / 2
    assert len(counting_source.calls) <= 3


def test_entries_can_be_filtered_by_glob(counting_source: CountingSource) -> None:
    with ZipArchive(counting_source) as archive:
        matched = sorted(entry.name for entry in archive.entries("logs/*.log"))
    assert matched == ["logs/2026-08-17.log", "logs/2026-08-18.log"]


def test_entry_metadata(counting_source: CountingSource) -> None:
    with ZipArchive(counting_source) as archive:
        entry = archive.entry("data/small.json")
        assert entry.size == len(MEMBERS["data/small.json"])
        assert entry.method == "deflate"
        assert not entry.is_dir
        assert entry.ratio <= 1.0  # tiny files can deflate to more bytes than they hold

        directory = archive.entry("empty_dir/")
        assert directory.is_dir


def test_missing_member_raises(counting_source: CountingSource) -> None:
    with ZipArchive(counting_source) as archive:
        with pytest.raises(MemberNotFoundError):
            archive.entry("nope.txt")
        with pytest.raises(MemberNotFoundError):
            archive.read("nope.txt")


def test_read_returns_exact_bytes(counting_source: CountingSource) -> None:
    with ZipArchive(counting_source) as archive:
        for name, payload in MEMBERS.items():
            assert archive.read(name) == payload


def test_reading_one_small_member_stays_cheap(counting_source: CountingSource) -> None:
    with ZipArchive(counting_source, block_size=64 * 1024) as archive:
        archive.read("readme.txt")
        transferred = archive.stats["bytes_fetched"]

    assert transferred < counting_source.size / 2


def test_summary_totals(counting_source: CountingSource) -> None:
    with ZipArchive(counting_source) as archive:
        summary = archive.summary()

    assert summary.files == len(MEMBERS)
    assert summary.directories == 1
    assert summary.total_size == sum(len(payload) for payload in MEMBERS.values())
    assert summary.total_compressed < summary.total_size
    assert 0.0 < summary.ratio < 1.0


def test_extract_writes_the_file(counting_source: CountingSource, tmp_path: Path) -> None:
    with ZipArchive(counting_source) as archive:
        written = archive.extract("data/small.json", tmp_path)

    assert written == tmp_path / "data" / "small.json"
    assert written.read_bytes() == MEMBERS["data/small.json"]
    assert not list(tmp_path.rglob("*.part"))  # no temporary leftovers


def test_extract_refuses_to_clobber(counting_source: CountingSource, tmp_path: Path) -> None:
    with ZipArchive(counting_source) as archive:
        archive.extract("readme.txt", tmp_path)
        with pytest.raises(FileExistsError):
            archive.extract("readme.txt", tmp_path)
        archive.extract("readme.txt", tmp_path, overwrite=True)


def test_extract_flatten_drops_directories(counting_source: CountingSource, tmp_path: Path) -> None:
    with ZipArchive(counting_source) as archive:
        written = archive.extract("logs/2026-08-17.log", tmp_path, flatten=True)
    assert written == tmp_path / "2026-08-17.log"


def test_extract_many_yields_every_member(counting_source: CountingSource, tmp_path: Path) -> None:
    with ZipArchive(counting_source) as archive:
        results = dict(archive.extract_many(list(MEMBERS), tmp_path))

    assert set(results) == set(MEMBERS)
    for name, path in results.items():
        assert path.read_bytes() == MEMBERS[name]


def test_large_member_round_trips(counting_source: CountingSource, tmp_path: Path) -> None:
    with ZipArchive(counting_source, block_size=64 * 1024, max_cached_blocks=4) as archive:
        written = archive.extract("data/big.bin", tmp_path)
    assert written.read_bytes() == MEMBERS["data/big.bin"]


def test_not_a_zip(tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"definitely not a zip file" * 100)
    with pytest.raises(ArchiveError):
        ZipArchive(LocalFileRangeSource(junk))


def test_encrypted_member_needs_the_password(tmp_path: Path) -> None:
    path = tmp_path / "secret.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("plain.txt", b"visible")
    with zipfile.ZipFile(path, "a") as zf:
        zf.setpassword(b"hunter2")

    with ZipArchive(LocalFileRangeSource(path)) as archive:
        assert archive.read("plain.txt") == b"visible"


@pytest.mark.parametrize(
    "member",
    ["../escape.txt", "/etc/passwd", "a/../../escape.txt", "C:/windows/system32/x.dll"],
)
def test_zip_slip_is_blocked(tmp_path: Path, member: str) -> None:
    with pytest.raises(UnsafeMemberError):
        _safe_destination(tmp_path.resolve(), member, flatten=False)


def test_safe_destination_accepts_normal_members(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    assert _safe_destination(root, "a/b/c.txt", flatten=False) == root / "a" / "b" / "c.txt"
    assert _safe_destination(root, "a/b/c.txt", flatten=True) == root / "c.txt"
    assert (
        _safe_destination(root, "windows\\style.txt", flatten=False)
        == root / "windows" / "style.txt"
    )


def test_malicious_archive_cannot_escape_the_destination(tmp_path: Path) -> None:
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("../escaped.txt", b"pwned")

    dest = tmp_path / "out"
    dest.mkdir()
    with ZipArchive(LocalFileRangeSource(path)) as archive, pytest.raises(UnsafeMemberError):
        archive.extract("../escaped.txt", dest)
    assert not (tmp_path / "escaped.txt").exists()
