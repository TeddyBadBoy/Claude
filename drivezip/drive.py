"""Google Drive backend: resolve file references and serve byte ranges from them."""

from __future__ import annotations

import random
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DriveApiError, RangeNotSupportedError
from .ranges import TransferStats

API_ROOT = "https://www.googleapis.com/drive/v3"
METADATA_FIELDS = "id,name,size,mimeType,md5Checksum,modifiedTime,driveId"
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5

_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")
_URL_PATTERNS = (
    re.compile(r"/file/d/([A-Za-z0-9_-]+)"),
    re.compile(r"/folders/([A-Za-z0-9_-]+)"),
    re.compile(r"/document/d/([A-Za-z0-9_-]+)"),
)


def parse_file_id(reference: str) -> str:
    """Accept a bare file id or any of the usual Drive share URLs, return the id.

    >>> parse_file_id("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQr/view?usp=sharing")
    '1AbCdEfGhIjKlMnOpQr'
    """
    reference = reference.strip()
    if not reference:
        raise ValueError("empty file reference")

    if _FILE_ID_RE.match(reference) and "/" not in reference:
        return reference

    parsed = urlparse(reference)
    if parsed.scheme in {"http", "https"}:
        for pattern in _URL_PATTERNS:
            match = pattern.search(parsed.path)
            if match:
                return match.group(1)
        query_id = parse_qs(parsed.query).get("id")
        if query_id:
            return query_id[0]

    raise ValueError(f"could not extract a Drive file id from: {reference!r}")


class DriveFile:
    """Metadata for one Drive object."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.id: str = payload["id"]
        self.name: str = payload.get("name", self.id)
        self.mime_type: str = payload.get("mimeType", "")
        self.md5: str | None = payload.get("md5Checksum")
        self.modified_time: str | None = payload.get("modifiedTime")
        self.drive_id: str | None = payload.get("driveId")
        raw_size = payload.get("size")
        if raw_size is None:
            raise DriveApiError(
                f"{self.name!r} has no byte size — Google-native documents "
                "(Docs/Sheets/Slides) and folders cannot be read as ZIP archives"
            )
        self.size = int(raw_size)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DriveFile(id={self.id!r}, name={self.name!r}, size={self.size})"


class DriveRangeSource:
    """A `RangeSource` that reads a Drive file through `files.get?alt=media`.

    Every read is a ranged GET, so opening a 40 GB archive costs a few hundred
    kilobytes rather than 40 GB.
    """

    def __init__(
        self,
        file_ref: str,
        *,
        credentials: Any = None,
        access_token: str | None = None,
        session: Any = None,
        timeout: float = 60.0,
    ) -> None:
        if credentials is None and access_token is None:
            raise ValueError("either credentials or access_token is required")

        self.file_id = parse_file_id(file_ref)
        self._credentials = credentials
        self._access_token = access_token
        self._timeout = timeout
        self._session = session if session is not None else self._new_session()
        self.stats = TransferStats()
        self.file = DriveFile(self._metadata())

    @staticmethod
    def _new_session() -> Any:
        import requests  # imported lazily so the package works without network extras

        return requests.Session()

    # -- HTTP plumbing -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        token = self._access_token or self._refreshed_token()
        return {"Authorization": f"Bearer {token}"}

    def _refreshed_token(self) -> str:
        credentials = self._credentials
        if not getattr(credentials, "valid", True):
            from google.auth.transport.requests import Request

            credentials.refresh(Request())
        token = getattr(credentials, "token", None)
        if not token:
            raise DriveApiError("credentials produced no access token")
        return str(token)

    def _get(self, url: str, *, params: dict[str, str], headers: dict[str, str]) -> Any:
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            merged = {**self._headers(), **headers}
            try:
                response = self._session.get(
                    url, params=params, headers=merged, timeout=self._timeout
                )
            except Exception as exc:  # network hiccup: retry with backoff
                last_error = exc
                self._sleep(attempt)
                continue

            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
                self._sleep(attempt)
                continue
            return response

        raise DriveApiError(f"request to {url} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    @staticmethod
    def _sleep(attempt: int) -> None:
        time.sleep(min(2**attempt, 16) * (0.5 + random.random() / 2))

    def _metadata(self) -> dict[str, Any]:
        response = self._get(
            f"{API_ROOT}/files/{self.file_id}",
            params={"fields": METADATA_FIELDS, "supportsAllDrives": "true"},
            headers={},
        )
        if response.status_code == 404:
            raise DriveApiError(
                f"file {self.file_id} not found, or the account in use cannot see it",
                status=404,
            )
        if response.status_code >= 400:
            raise DriveApiError(
                f"metadata lookup failed with HTTP {response.status_code}",
                status=response.status_code,
                body=_snippet(response),
            )
        return dict(response.json())

    # -- RangeSource -------------------------------------------------------

    @property
    def name(self) -> str:
        return self.file.name

    @property
    def size(self) -> int:
        return self.file.size

    def fetch(self, start: int, end: int) -> bytes:
        if start < 0 or end < start:
            raise ValueError(f"invalid range: {start}-{end}")
        end = min(end, self.size - 1)

        response = self._get(
            f"{API_ROOT}/files/{self.file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers={"Range": f"bytes={start}-{end}"},
        )

        if response.status_code == 416:
            return b""
        if response.status_code >= 400:
            raise DriveApiError(
                f"range request {start}-{end} failed with HTTP {response.status_code}",
                status=response.status_code,
                body=_snippet(response),
            )

        data = response.content
        if response.status_code == 200 and len(data) > (end - start + 1):
            # The endpoint ignored the Range header and sent the whole object.
            if start == 0:
                data = data[: end + 1]
            else:
                raise RangeNotSupportedError(
                    "Drive returned the full object instead of the requested range; "
                    "ranged reads are unavailable for this file"
                )

        self.stats.record_fetch(len(data))
        return data

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> DriveRangeSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _snippet(response: Any, limit: int = 400) -> str:
    try:
        return str(response.text)[:limit]
    except Exception:  # pragma: no cover - defensive
        return "<unreadable response body>"
