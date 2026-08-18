"""ZIP-level operations on top of a byte-range source."""

from __future__ import annotations

import fnmatch
import posixpath
import shutil
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from .errors import ArchiveError, MemberNotFoundError, UnsafeMemberError
from .ranges import RangeSource
from .reader import (
    DEFAULT_BLOCK_SIZE,
    DEFAULT_MAX_CACHED_BLOCKS,
    DEFAULT_READAHEAD_BLOCKS,
    RangeReader,
)

# The end-of-central-directory record plus, for most archives, the whole central
# directory lives in the last stretch of the file. Grabbing it in one request
# turns "list a remote archive" into a single round trip.
DEFAULT_TAIL_PREFETCH = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class Entry:
    """One member of an archive."""

    name: str
    size: int
    compressed_size: int
    compress_type: int
    crc: int
    modified: datetime | None
    is_dir: bool

    @property
    def ratio(self) -> float:
        if self.size == 0:
            return 0.0
        return 1.0 - (self.compressed_size / self.size)

    @property
    def method(self) -> str:
        return _COMPRESSION_NAMES.get(self.compress_type, f"method-{self.compress_type}")


_COMPRESSION_NAMES = {
    zipfile.ZIP_STORED: "stored",
    zipfile.ZIP_DEFLATED: "deflate",
    zipfile.ZIP_BZIP2: "bzip2",
    zipfile.ZIP_LZMA: "lzma",
}


@dataclass(frozen=True)
class Summary:
    """Aggregate numbers for a whole archive."""

    name: str
    archive_size: int
    entries: int
    files: int
    directories: int
    total_size: int
    total_compressed: int

    @property
    def ratio(self) -> float:
        if self.total_size == 0:
            return 0.0
        return 1.0 - (self.total_compressed / self.total_size)


class ZipArchive:
    """Read a ZIP archive that lives behind a `RangeSource`.

    Only the central directory and the members actually requested are
    transferred, so listing a 40 GB archive costs about a megabyte.
    """

    def __init__(
        self,
        source: RangeSource,
        *,
        block_size: int = DEFAULT_BLOCK_SIZE,
        max_cached_blocks: int = DEFAULT_MAX_CACHED_BLOCKS,
        readahead_blocks: int = DEFAULT_READAHEAD_BLOCKS,
        tail_prefetch: int = DEFAULT_TAIL_PREFETCH,
        close_source: bool = True,
    ) -> None:
        self._source = source
        self._close_source = close_source
        self._reader = RangeReader(
            source,
            block_size=block_size,
            max_cached_blocks=max_cached_blocks,
            readahead_blocks=readahead_blocks,
        )
        self._reader.prefetch_tail(tail_prefetch)
        try:
            self._zip = zipfile.ZipFile(self._reader)
        except zipfile.BadZipFile as exc:
            raise ArchiveError(f"{source.name} is not a readable ZIP archive: {exc}") from exc

    # -- introspection -----------------------------------------------------

    @property
    def stats(self) -> dict[str, int]:
        stats = self._reader.stats.as_dict()
        stats["archive_size"] = self._reader.size
        return stats

    @property
    def name(self) -> str:
        return self._source.name

    def entries(self, pattern: str | None = None) -> list[Entry]:
        """Every member, optionally filtered by a glob such as `logs/*.json`."""
        result = [_to_entry(info) for info in self._zip.infolist()]
        if pattern is not None:
            result = [entry for entry in result if fnmatch.fnmatch(entry.name, pattern)]
        return result

    def entry(self, name: str) -> Entry:
        try:
            return _to_entry(self._zip.getinfo(name))
        except KeyError as exc:
            raise MemberNotFoundError(f"{name!r} is not in {self._source.name}") from exc

    def summary(self) -> Summary:
        entries = self.entries()
        files = [entry for entry in entries if not entry.is_dir]
        return Summary(
            name=self._source.name,
            archive_size=self._reader.size,
            entries=len(entries),
            files=len(files),
            directories=len(entries) - len(files),
            total_size=sum(entry.size for entry in files),
            total_compressed=sum(entry.compressed_size for entry in files),
        )

    # -- reading -----------------------------------------------------------

    def open(self, name: str, *, pwd: bytes | None = None) -> IO[bytes]:
        """Open one member as a binary stream, fetching only its byte range."""
        info = self._info(name)
        # Local header + payload sit next to each other; pulling them in one
        # sweep keeps a member extraction close to a single request.
        self._reader.prefetch(info.header_offset, info.compress_size + len(info.filename) + 1024)
        return self._zip.open(info, "r", pwd=pwd)

    def read(self, name: str, *, pwd: bytes | None = None) -> bytes:
        with self.open(name, pwd=pwd) as handle:
            return handle.read()

    def extract(
        self,
        name: str,
        dest_dir: str | Path,
        *,
        overwrite: bool = False,
        flatten: bool = False,
        pwd: bytes | None = None,
    ) -> Path:
        """Write one member below `dest_dir` and return the path written."""
        info = self._info(name)
        root = Path(dest_dir).resolve()
        target = _safe_destination(root, info.filename, flatten=flatten)

        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            return target

        if target.exists() and not overwrite:
            raise FileExistsError(f"{target} already exists (pass overwrite to replace it)")

        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with self.open(name, pwd=pwd) as src, partial.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
        partial.replace(target)
        return target

    def extract_many(
        self,
        names: list[str],
        dest_dir: str | Path,
        *,
        overwrite: bool = False,
        flatten: bool = False,
        pwd: bytes | None = None,
    ) -> Iterator[tuple[str, Path]]:
        """Extract several members in archive order, yielding each as it lands.

        Sorting by header offset keeps reads moving forward through the archive,
        which is what the block cache and read-ahead are tuned for.
        """
        ordered = sorted(names, key=lambda name: self._info(name).header_offset)
        for name in ordered:
            yield name, self.extract(name, dest_dir, overwrite=overwrite, flatten=flatten, pwd=pwd)

    # -- lifecycle ---------------------------------------------------------

    def _info(self, name: str) -> zipfile.ZipInfo:
        try:
            return self._zip.getinfo(name)
        except KeyError as exc:
            raise MemberNotFoundError(f"{name!r} is not in {self._source.name}") from exc

    def close(self) -> None:
        self._zip.close()
        self._reader.close()
        if self._close_source:
            self._source.close()

    def __enter__(self) -> ZipArchive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _to_entry(info: zipfile.ZipInfo) -> Entry:
    modified: datetime | None
    try:
        modified = datetime(*info.date_time)
    except (ValueError, TypeError):
        modified = None
    return Entry(
        name=info.filename,
        size=info.file_size,
        compressed_size=info.compress_size,
        compress_type=info.compress_type,
        crc=info.CRC,
        modified=modified,
        is_dir=info.is_dir(),
    )


def _safe_destination(root: Path, member: str, *, flatten: bool) -> Path:
    """Resolve a member name below `root`, refusing anything that escapes it.

    Archives can carry absolute paths, `..` segments or Windows drive letters —
    the "zip slip" family of bugs. Everything is normalised and then checked
    against the destination root before a single byte is written.
    """
    normalised = member.replace("\\", "/")
    if flatten:
        normalised = posixpath.basename(normalised.rstrip("/"))
        if not normalised:
            raise UnsafeMemberError(f"member {member!r} has no file name to flatten to")

    if normalised.startswith("/") or (len(normalised) > 1 and normalised[1] == ":"):
        raise UnsafeMemberError(f"member {member!r} uses an absolute path")

    target = (root / normalised).resolve()
    if target != root and root not in target.parents:
        raise UnsafeMemberError(f"member {member!r} would be written outside {root}")
    return target


def open_archive(source: RangeSource, **kwargs: Any) -> ZipArchive:
    """Convenience wrapper mirroring `ZipArchive(...)`."""
    return ZipArchive(source, **kwargs)
