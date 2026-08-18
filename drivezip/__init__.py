"""Work with large ZIP archives on Google Drive without downloading them.

The central directory of a ZIP lives at the end of the file, so with HTTP range
requests an archive can be listed and selectively extracted while only a
fraction of its bytes ever cross the network.

    from drivezip import DriveRangeSource, ZipArchive

    with ZipArchive(DriveRangeSource(file_id, access_token=token)) as archive:
        for entry in archive.entries("logs/*.json"):
            print(entry.name, entry.size)
        archive.extract("logs/2026-08-18.json", "./out")
"""

from __future__ import annotations

from .archive import Entry, Summary, ZipArchive, open_archive
from .errors import (
    ArchiveError,
    AuthError,
    DriveApiError,
    DriveZipError,
    MemberNotFoundError,
    RangeNotSupportedError,
    UnsafeMemberError,
)
from .ranges import LocalFileRangeSource, RangeSource, TransferStats
from .reader import RangeReader

__version__ = "0.1.0"

__all__ = [
    "ArchiveError",
    "AuthError",
    "DriveApiError",
    "DriveZipError",
    "Entry",
    "LocalFileRangeSource",
    "MemberNotFoundError",
    "RangeNotSupportedError",
    "RangeReader",
    "RangeSource",
    "Summary",
    "TransferStats",
    "UnsafeMemberError",
    "ZipArchive",
    "__version__",
    "open_archive",
]


def __getattr__(name: str) -> object:
    # The Drive backend pulls in `requests`; keep it out of the import path for
    # anyone using this package against local archives only.
    if name in {"DriveRangeSource", "DriveFile", "parse_file_id"}:
        from . import drive

        return getattr(drive, name)
    if name in {"HttpRangeSource", "drive_public_url"}:
        from . import http

        return getattr(http, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
