"""A seekable, read-only file object built out of HTTP byte-range requests.

`zipfile.ZipFile` needs nothing more than a seekable binary stream, so wrapping
a `RangeSource` in one lets the standard library parse a multi-gigabyte remote
archive while only the bytes it actually touches cross the wire.
"""

from __future__ import annotations

import io
from collections import OrderedDict

from .ranges import RangeSource, TransferStats

DEFAULT_BLOCK_SIZE = 1 << 20  # 1 MiB
DEFAULT_MAX_CACHED_BLOCKS = 32  # 32 MiB of resident cache at the default block size
DEFAULT_READAHEAD_BLOCKS = 3  # extra blocks pulled per miss, so sequential reads batch up


class RangeReader(io.RawIOBase):
    """Expose a `RangeSource` as a seekable stream with an LRU block cache."""

    def __init__(
        self,
        source: RangeSource,
        *,
        block_size: int = DEFAULT_BLOCK_SIZE,
        max_cached_blocks: int = DEFAULT_MAX_CACHED_BLOCKS,
        readahead_blocks: int = DEFAULT_READAHEAD_BLOCKS,
    ) -> None:
        super().__init__()
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        if max_cached_blocks <= 0:
            raise ValueError("max_cached_blocks must be positive")
        if readahead_blocks < 0:
            raise ValueError("readahead_blocks must not be negative")

        self._source = source
        self._size = source.size
        self._block_size = block_size
        self._readahead = readahead_blocks
        # Never let a single fetch exceed what the cache can hold, or it would
        # evict its own blocks before the caller reads them.
        self._max_blocks = max(max_cached_blocks, readahead_blocks + 1)
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._pos = 0
        self.stats = TransferStats()

    # -- stream protocol ---------------------------------------------------

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    @property
    def size(self) -> int:
        return self._size

    @property
    def name(self) -> str:
        return self._source.name

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if target < 0:
            raise OSError("negative seek position")
        self._pos = target
        return self._pos

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        view = memoryview(buffer).cast("B")
        want = min(len(view), max(self._size - self._pos, 0))
        if want == 0:
            return 0

        written = 0
        while written < want:
            offset = self._pos + written
            block_index = offset // self._block_size
            block = self._block(block_index)
            inside = offset - block_index * self._block_size
            chunk = block[inside : inside + (want - written)]
            if not chunk:
                break
            view[written : written + len(chunk)] = chunk
            written += len(chunk)

        self._pos += written
        return written

    # -- caching -----------------------------------------------------------

    def _block(self, index: int) -> bytes:
        cached = self._cache.get(index)
        if cached is not None:
            self._cache.move_to_end(index)
            self.stats.record_cache(hit=True)
            return cached

        self.stats.record_cache(hit=False)
        self._fetch_blocks(index, index + self._readahead)
        return self._cache.get(index, b"")

    def _fetch_blocks(self, first: int, last: int) -> None:
        """Fetch blocks [first, last] in one request, skipping trailing cached ones."""
        total_blocks = (self._size + self._block_size - 1) // self._block_size
        last = min(last, max(total_blocks - 1, 0))
        while last > first and last in self._cache:
            last -= 1

        start = first * self._block_size
        end = min((last + 1) * self._block_size, self._size) - 1
        if end < start:
            return

        data = self._source.fetch(start, end)
        self.stats.record_fetch(len(data))

        for offset in range(0, len(data), self._block_size):
            self._store(
                first + offset // self._block_size, data[offset : offset + self._block_size]
            )

    def _store(self, index: int, data: bytes) -> None:
        self._cache[index] = data
        self._cache.move_to_end(index)
        while len(self._cache) > self._max_blocks:
            self._cache.popitem(last=False)

    def prefetch(self, start: int, length: int) -> None:
        """Warm the cache for a span, coalescing it into as few requests as possible."""
        if length <= 0 or self._size == 0:
            return
        start = max(start, 0)
        end = min(start + length, self._size) - 1
        if end < start:
            return

        first = start // self._block_size
        last = end // self._block_size
        index = first
        while index <= last:
            if index in self._cache:
                index += 1
                continue
            run_end = index
            while run_end + 1 <= last and (run_end + 1) not in self._cache:
                run_end += 1
            self._fetch_blocks(index, run_end)
            index = run_end + 1

    def prefetch_tail(self, length: int) -> None:
        """Warm the last `length` bytes — where a ZIP keeps its central directory."""
        if length <= 0:
            return
        self.prefetch(max(self._size - length, 0), min(length, self._size))

    def close(self) -> None:
        if not self.closed:
            self._cache.clear()
        super().close()
