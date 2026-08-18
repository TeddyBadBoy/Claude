"""Byte-range sources: the minimal interface the reader needs from a backend."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class TransferStats:
    """Counters describing how much data a session actually moved."""

    requests: int = 0
    bytes_fetched: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record_fetch(self, nbytes: int) -> None:
        with self._lock:
            self.requests += 1
            self.bytes_fetched += nbytes

    def record_cache(self, *, hit: bool) -> None:
        with self._lock:
            if hit:
                self.cache_hits += 1
            else:
                self.cache_misses += 1

    def as_dict(self) -> dict[str, int]:
        return {
            "requests": self.requests,
            "bytes_fetched": self.bytes_fetched,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
        }


@runtime_checkable
class RangeSource(Protocol):
    """A remote or local object that can serve arbitrary byte ranges."""

    @property
    def name(self) -> str:
        """Human-readable identifier, used in messages."""

    @property
    def size(self) -> int:
        """Total size of the object in bytes."""

    def fetch(self, start: int, end: int) -> bytes:
        """Return bytes [start, end] inclusive, as HTTP Range semantics define them."""

    def close(self) -> None:
        """Release any underlying resources."""


class LocalFileRangeSource:
    """A `RangeSource` backed by a file on disk.

    Useful on its own (the CLI accepts local archives) and as the backend the
    test suite uses to exercise everything above it without touching a network.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        if not self._path.is_file():
            raise FileNotFoundError(f"no such file: {self._path}")
        self._size = self._path.stat().st_size
        self._fh = self._path.open("rb")
        self.stats = TransferStats()

    @property
    def name(self) -> str:
        return str(self._path)

    @property
    def size(self) -> int:
        return self._size

    def fetch(self, start: int, end: int) -> bytes:
        if start < 0 or end < start:
            raise ValueError(f"invalid range: {start}-{end}")
        self._fh.seek(start)
        data = self._fh.read(end - start + 1)
        self.stats.record_fetch(len(data))
        return data

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> LocalFileRangeSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
