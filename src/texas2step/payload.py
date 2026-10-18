"""Incompressible payload for download tests, generated once and streamed from memory."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator

CHUNK = 1 << 20  # 1 MiB per write keeps per-chunk overhead negligible at multi-gigabit rates
_POOL = [os.urandom(CHUNK) for _ in range(8)]


def chunk(n: int, index: int = 0) -> bytes:
    """Return ``n`` (<= CHUNK) random bytes without allocating new random data."""
    block = _POOL[index % len(_POOL)]
    return block if n == CHUNK else block[:n]


async def stream_bytes(total: int) -> AsyncIterator[bytes]:
    """Yield exactly ``total`` bytes."""
    sent, i = 0, 0
    while sent < total:
        n = min(CHUNK, total - sent)
        yield chunk(n, i)
        sent += n
        i += 1


async def stream_for(seconds: float) -> AsyncIterator[bytes]:
    """Yield data until ``seconds`` elapse."""
    deadline = time.monotonic() + seconds
    i = 0
    while time.monotonic() < deadline:
        yield chunk(CHUNK, i)
        i += 1
        if i % 64 == 0:
            await asyncio.sleep(0)  # let other connections run on very fast links
