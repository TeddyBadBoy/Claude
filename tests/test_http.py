from __future__ import annotations

import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from helpers import MEMBERS

from drivezip.archive import ZipArchive
from drivezip.errors import DriveApiError, RangeNotSupportedError
from drivezip.http import HttpRangeSource, drive_public_url

RANGE_RE = re.compile(r"bytes=(\d+)-(\d+)")


def make_handler(payload: bytes, *, honour_ranges: bool = True, filename: str = "served.zip"):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            header = self.headers.get("Range", "")
            match = RANGE_RE.match(header)
            if not honour_ranges or not match:
                body = payload
                self.send_response(200)
            else:
                start, end = int(match.group(1)), int(match.group(2))
                if start >= len(payload):
                    self.send_response(416)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, len(payload) - 1)
                body = payload[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")

            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass  # keep the test output quiet

    return Handler


def serve(payload: bytes, **kwargs: object) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(payload, **kwargs))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/archive.zip"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def archive_url(archive_path: Path) -> Iterator[str]:
    yield from serve(archive_path.read_bytes())


def test_probe_discovers_size_and_name(archive_url: str, archive_path: Path) -> None:
    with HttpRangeSource(archive_url) as source:
        assert source.size == archive_path.stat().st_size
        assert source.name == "served.zip"


def test_fetch_returns_the_requested_range(archive_url: str, archive_path: Path) -> None:
    raw = archive_path.read_bytes()
    with HttpRangeSource(archive_url) as source:
        assert source.fetch(100, 199) == raw[100:200]
        assert source.fetch(source.size - 10, source.size + 500) == raw[-10:]
        assert source.stats.bytes_fetched == 110


def test_fetch_rejects_backwards_ranges(archive_url: str) -> None:
    with HttpRangeSource(archive_url) as source, pytest.raises(ValueError):
        source.fetch(200, 100)


def test_reading_an_archive_over_http(archive_url: str, tmp_path: Path) -> None:
    """End to end over a real socket: HTTP ranges -> reader -> zipfile -> bytes."""
    with ZipArchive(HttpRangeSource(archive_url)) as archive:
        assert {entry.name for entry in archive.entries()} >= set(MEMBERS)
        assert archive.read("data/small.json") == MEMBERS["data/small.json"]

        written = archive.extract("logs/2026-08-17.log", tmp_path)
        assert written.read_bytes() == MEMBERS["logs/2026-08-17.log"]

        stats = archive.stats
        assert stats["bytes_fetched"] < stats["archive_size"] / 2


def test_server_ignoring_ranges_is_refused(archive_path: Path) -> None:
    for url in serve(archive_path.read_bytes(), honour_ranges=False):
        with pytest.raises(RangeNotSupportedError):
            HttpRangeSource(url)


def test_html_response_is_explained(tmp_path: Path) -> None:
    """Drive serves an interstitial page when a file is not actually public."""

    class HtmlHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            body = b"<html>Google Drive - Virus scan warning</html>"
            self.send_response(206)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Range", f"bytes 0-0/{len(body)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), HtmlHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(DriveApiError, match="not shared publicly"):
            HttpRangeSource(f"http://127.0.0.1:{server.server_port}/x.zip")
    finally:
        server.shutdown()
        server.server_close()


def test_public_drive_url_shape() -> None:
    url = drive_public_url("1cotRGgtJz9Ig4DEDBwy2eHE5W3E5R38S")
    assert url.startswith("https://drive.usercontent.google.com/download?")
    assert "id=1cotRGgtJz9Ig4DEDBwy2eHE5W3E5R38S" in url
    assert "confirm=t" in url  # skips the virus-scan interstitial for large files
