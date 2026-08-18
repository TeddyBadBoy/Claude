"""Test doubles and fixture payloads shared across the suite."""

from __future__ import annotations

import random

from drivezip.ranges import LocalFileRangeSource

# `big.bin` is deliberately incompressible, so the fixture archive is several
# megabytes and "we only fetched a fraction of it" assertions mean something.
MEMBERS: dict[str, bytes] = {
    "readme.txt": b"drivezip test archive\n",
    "data/small.json": b'{"hello": "world"}\n',
    "data/big.bin": random.Random(20260818).randbytes(6 << 20),
    "logs/2026-08-17.log": b"line\n" * 5000,
    "logs/2026-08-18.log": b"another line\n" * 5000,
}


class CountingSource:
    """Wraps a source and records every range served, so tests can assert on I/O."""

    def __init__(self, inner: LocalFileRangeSource) -> None:
        self._inner = inner
        self.calls: list[tuple[int, int]] = []

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def size(self) -> int:
        return self._inner.size

    @property
    def bytes_served(self) -> int:
        return sum(end - start + 1 for start, end in self.calls)

    def fetch(self, start: int, end: int) -> bytes:
        self.calls.append((start, end))
        return self._inner.fetch(start, end)

    def close(self) -> None:
        self._inner.close()
