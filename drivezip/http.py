"""Read ZIP archives over plain HTTP(S) byte ranges.

Anything that answers `Range` requests works: a static file server, an object
store, a CDN — or a Google Drive file shared with "anyone with the link", which
can be read this way without credentials at all.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any
from urllib.parse import unquote, urlparse

from .errors import DriveApiError, RangeNotSupportedError
from .ranges import TransferStats

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
_CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+)")
_FILENAME_RE = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", re.IGNORECASE)


def drive_public_url(file_id: str) -> str:
    """The direct-download endpoint for a Drive file shared with anyone.

    `confirm=t` skips the virus-scan interstitial that Drive shows for large
    files; without it the response is an HTML page rather than the archive.
    """
    return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"


class HttpRangeSource:
    """A `RangeSource` over any endpoint that honours HTTP `Range`."""

    def __init__(
        self,
        url: str,
        *,
        session: Any = None,
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
        name: str | None = None,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._headers = dict(headers or {})
        self._session = session if session is not None else self._new_session()
        self.stats = TransferStats()
        self._size, discovered = self._probe()
        self._name = name or discovered

    @staticmethod
    def _new_session() -> Any:
        import requests

        return requests.Session()

    # -- discovery ---------------------------------------------------------

    def _probe(self) -> tuple[int, str]:
        """One tiny ranged GET tells us the total size and the server's manners."""
        response = self._get({"Range": "bytes=0-0"})

        if response.status_code == 200:
            raise RangeNotSupportedError(
                f"{self._url} answered a range request with the whole body; "
                "reading it selectively is not possible"
            )
        if response.status_code >= 400:
            raise DriveApiError(
                f"probe of {self._url} failed with HTTP {response.status_code}",
                status=response.status_code,
            )

        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("text/html"):
            raise DriveApiError(
                f"{self._url} returned an HTML page instead of file content — "
                "the file is probably not shared publicly, or the link is wrong"
            )

        match = _CONTENT_RANGE_RE.search(response.headers.get("Content-Range", ""))
        if not match:
            raise RangeNotSupportedError(
                f"{self._url} answered {response.status_code} without a usable "
                "Content-Range header, so its size is unknown"
            )

        return int(match.group(1)), self._filename(response)

    def _filename(self, response: Any) -> str:
        disposition = response.headers.get("Content-Disposition", "")
        match = _FILENAME_RE.search(disposition)
        if match:
            return unquote(match.group(1))
        path = urlparse(self._url).path
        return path.rsplit("/", 1)[-1] or self._url

    # -- HTTP plumbing -----------------------------------------------------

    def _get(self, headers: dict[str, str]) -> Any:
        merged = {**self._headers, **headers}
        last_error: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self._session.get(self._url, headers=merged, timeout=self._timeout)
            except Exception as exc:
                last_error = exc
                self._sleep(attempt)
                continue

            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                self._sleep(attempt)
                continue
            return response

        raise DriveApiError(
            f"request to {self._url} failed after {MAX_ATTEMPTS} attempts: {last_error}"
        )

    @staticmethod
    def _sleep(attempt: int) -> None:
        time.sleep(min(2**attempt, 16) * (0.5 + random.random() / 2))

    # -- RangeSource -------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def size(self) -> int:
        return self._size

    def fetch(self, start: int, end: int) -> bytes:
        if start < 0 or end < start:
            raise ValueError(f"invalid range: {start}-{end}")
        end = min(end, self._size - 1)

        response = self._get({"Range": f"bytes={start}-{end}"})
        if response.status_code == 416:
            return b""
        if response.status_code >= 400:
            raise DriveApiError(
                f"range request {start}-{end} failed with HTTP {response.status_code}",
                status=response.status_code,
            )

        data = response.content
        if response.status_code == 200 and len(data) > (end - start + 1):
            if start == 0:
                data = data[: end + 1]
            else:
                raise RangeNotSupportedError(
                    f"{self._url} ignored the Range header mid-file; "
                    "reading it selectively is not possible"
                )

        self.stats.record_fetch(len(data))
        return data

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> HttpRangeSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
