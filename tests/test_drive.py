from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from helpers import MEMBERS

from drivezip.archive import ZipArchive
from drivezip.drive import DriveRangeSource, parse_file_id
from drivezip.errors import DriveApiError, RangeNotSupportedError

FILE_ID = "1AbCdEfGhIjKlMnOpQrStU"


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (FILE_ID, FILE_ID),
        (f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing", FILE_ID),
        (f"https://drive.google.com/open?id={FILE_ID}", FILE_ID),
        (f"https://drive.google.com/uc?export=download&id={FILE_ID}", FILE_ID),
        (f"  https://drive.google.com/file/d/{FILE_ID}/edit  ", FILE_ID),
    ],
)
def test_parse_file_id(reference: str, expected: str) -> None:
    assert parse_file_id(reference) == expected


@pytest.mark.parametrize("reference", ["", "   ", "not a link", "https://example.com/whatever"])
def test_parse_file_id_rejects_junk(reference: str) -> None:
    with pytest.raises(ValueError):
        parse_file_id(reference)


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", payload: Any = None) -> None:
        self.status_code = status_code
        self.content = content
        self._payload = payload

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        if self._payload is None:
            return json.loads(self.text)
        return self._payload


class FakeSession:
    """Serves Drive metadata and honours Range headers against a local file."""

    def __init__(self, path: Path, *, name: str = "sample.zip") -> None:
        self._data = path.read_bytes()
        self._name = name
        self.requests: list[dict[str, Any]] = []
        self.scripted: list[FakeResponse] = []
        self.ignore_range = False
        self.closed = False

    def get(self, url: str, *, params: dict[str, str], headers: dict[str, str], timeout: float):
        self.requests.append({"url": url, "params": params, "headers": headers})
        if self.scripted:
            return self.scripted.pop(0)

        assert headers["Authorization"].startswith("Bearer ")
        if params.get("alt") != "media":
            return FakeResponse(
                200,
                payload={
                    "id": FILE_ID,
                    "name": self._name,
                    "size": str(len(self._data)),
                    "mimeType": "application/zip",
                },
            )

        if self.ignore_range:
            return FakeResponse(200, self._data)

        start, end = _parse_range(headers["Range"])
        if start >= len(self._data):
            return FakeResponse(416)
        return FakeResponse(206, self._data[start : end + 1])

    def close(self) -> None:
        self.closed = True


def _parse_range(header: str) -> tuple[int, int]:
    start, _, end = header.removeprefix("bytes=").partition("-")
    return int(start), int(end)


@pytest.fixture
def fake_session(archive_path: Path) -> FakeSession:
    return FakeSession(archive_path)


def test_metadata_is_read_once(fake_session: FakeSession) -> None:
    source = DriveRangeSource(FILE_ID, access_token="token", session=fake_session)
    assert source.name == "sample.zip"
    assert source.size == len(fake_session._data)
    assert len(fake_session.requests) == 1
    assert fake_session.requests[0]["params"]["supportsAllDrives"] == "true"


def test_fetch_returns_the_requested_range(fake_session: FakeSession) -> None:
    source = DriveRangeSource(FILE_ID, access_token="token", session=fake_session)
    assert source.fetch(10, 19) == fake_session._data[10:20]
    assert source.stats.bytes_fetched == 10


def test_fetch_clamps_past_the_end(fake_session: FakeSession) -> None:
    source = DriveRangeSource(FILE_ID, access_token="token", session=fake_session)
    tail = source.fetch(source.size - 5, source.size + 500)
    assert tail == fake_session._data[-5:]


def test_fetch_rejects_backwards_ranges(fake_session: FakeSession) -> None:
    source = DriveRangeSource(FILE_ID, access_token="token", session=fake_session)
    with pytest.raises(ValueError):
        source.fetch(50, 10)


def test_range_past_eof_is_empty(fake_session: FakeSession) -> None:
    source = DriveRangeSource(FILE_ID, access_token="token", session=fake_session)
    fake_session.scripted = [FakeResponse(416)]
    assert source.fetch(0, 10) == b""


def test_ignored_range_header_is_detected(fake_session: FakeSession) -> None:
    source = DriveRangeSource(FILE_ID, access_token="token", session=fake_session)
    fake_session.ignore_range = True

    assert source.fetch(0, 9) == fake_session._data[:10]  # a leading range can be salvaged
    with pytest.raises(RangeNotSupportedError):
        source.fetch(100, 199)


def test_missing_size_is_reported_clearly(archive_path: Path) -> None:
    session = FakeSession(archive_path)
    session.scripted = [
        FakeResponse(
            200,
            payload={
                "id": FILE_ID,
                "name": "Notes",
                "mimeType": "application/vnd.google-apps.document",
            },
        )
    ]
    with pytest.raises(DriveApiError, match="no byte size"):
        DriveRangeSource(FILE_ID, access_token="token", session=session)


def test_not_found_is_reported_clearly(archive_path: Path) -> None:
    session = FakeSession(archive_path)
    session.scripted = [FakeResponse(404, b"not found")]
    with pytest.raises(DriveApiError, match="not found"):
        DriveRangeSource(FILE_ID, access_token="token", session=session)


def test_transient_failures_are_retried(
    archive_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DriveRangeSource, "_sleep", staticmethod(lambda attempt: None))
    session = FakeSession(archive_path)
    session.scripted = [FakeResponse(503, b"try later"), FakeResponse(429, b"slow down")]

    source = DriveRangeSource(FILE_ID, access_token="token", session=session)
    assert source.size == len(session._data)
    assert len(session.requests) == 3


def test_credentials_are_required() -> None:
    with pytest.raises(ValueError):
        DriveRangeSource(FILE_ID)


def test_close_releases_the_session(fake_session: FakeSession) -> None:
    with DriveRangeSource(FILE_ID, access_token="token", session=fake_session):
        pass
    assert fake_session.closed


def test_end_to_end_over_the_fake_drive(fake_session: FakeSession, tmp_path: Path) -> None:
    """The whole stack: Drive ranges -> reader -> zipfile -> extracted bytes."""
    source = DriveRangeSource(FILE_ID, access_token="token", session=fake_session)
    with ZipArchive(source) as archive:
        assert {entry.name for entry in archive.entries()} >= set(MEMBERS)
        assert archive.read("data/small.json") == MEMBERS["data/small.json"]
        written = archive.extract("logs/2026-08-18.log", tmp_path)
        assert written.read_bytes() == MEMBERS["logs/2026-08-18.log"]

        stats = archive.stats
        assert stats["bytes_fetched"] < stats["archive_size"] / 2
