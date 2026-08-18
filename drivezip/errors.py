"""Exception types raised by drivezip."""

from __future__ import annotations


class DriveZipError(Exception):
    """Base class for every error raised by this package."""


class AuthError(DriveZipError):
    """Credentials are missing, malformed or insufficient."""


class DriveApiError(DriveZipError):
    """The Drive API returned an unexpected response."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class RangeNotSupportedError(DriveZipError):
    """The remote endpoint ignored or rejected a byte-range request."""


class ArchiveError(DriveZipError):
    """The remote object is not a usable ZIP archive."""


class MemberNotFoundError(DriveZipError):
    """A requested archive member does not exist."""


class UnsafeMemberError(DriveZipError):
    """An archive member would be written outside the destination directory."""
