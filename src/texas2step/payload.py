"""What the download tests send: five founding documents as text, then random bytes,
generated once and streamed from memory (docs/methodology.md#what-the-tests-send)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

CHUNK = 1 << 20  # 1 MiB per write keeps per-chunk overhead negligible at multi-gigabit rates
_POOL = [os.urandom(CHUNK) for _ in range(8)]
# Each download starts with these, in the order they were written, unless
# download_documents is off: public domain, and readable to anyone who decrypts a test.
DOCUMENTS = (
    "declaration-of-independence.md",
    "constitution-of-the-united-states.md",
    "texas-declaration-of-independence.md",
    "constitution-of-the-republic-of-texas.md",
    "texas-constitution.md",
)
TEXT = b"\n".join((Path(__file__).parent / "documents" / name).read_bytes() for name in DOCUMENTS)


def chunk(n: int, index: int = 0) -> bytes:
    """Return ``n`` (<= CHUNK) random bytes without allocating new random data."""
    block = _POOL[index % len(_POOL)]
    return block if n == CHUNK else block[:n]


def span(offset: int, n: int) -> bytes:
    """``n`` (<= CHUNK) bytes of the pool from ``offset``, wrapping at its end: a stream
    that asks for each span from where the last one ended walks the whole pool."""
    block, at = divmod(offset % (CHUNK * len(_POOL)), CHUNK)
    data = _POOL[block][at : at + n]
    if len(data) < n:
        data += _POOL[(block + 1) % len(_POOL)][: n - len(data)]
    return data


async def stream_bytes(total: int, text: bytes = b"") -> AsyncIterator[bytes]:
    """Yield exactly ``total`` bytes: ``text`` first, as much of it as fits, then random."""
    sent, i = 0, 0
    if text and total:
        head = text[:total]
        yield head
        sent = len(head)
    while sent < total:
        n = min(CHUNK, total - sent)
        yield chunk(n, i)
        sent += n
        i += 1


async def stream_for(seconds: float, text: bytes = b"") -> AsyncIterator[bytes]:
    """Yield ``text``, then random data until ``seconds`` elapse."""
    deadline = time.monotonic() + seconds
    if text:
        yield text
    i = 0
    while time.monotonic() < deadline:
        yield chunk(CHUNK, i)
        i += 1
        if i % 64 == 0:
            await asyncio.sleep(0)  # let other connections run on very fast links
