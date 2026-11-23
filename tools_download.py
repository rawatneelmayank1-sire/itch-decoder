"""Resumable byte-range downloader for a prefix of a real NASDAQ ITCH 5.0 file.

The published files are ~4.8 GB gzip (~11 GB raw), which is impractical to fetch
in full. gzip is a *stream* format, so a byte-range prefix of the .gz decodes to a
valid prefix of the underlying ITCH byte stream -- genuine exchange data, just
truncated in time. We stop at a whole-message boundary.

Resumable by design: the compressed prefix is appended to on disk and the HTTP
Range request continues from the current file size, so a crash costs one chunk.
"""
from __future__ import annotations

import os
import socket
import sys
import time
import urllib.request
import zlib

URL = "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/01302019.NASDAQ_ITCH50.gz"
HERE = os.path.dirname(os.path.abspath(__file__))
GZ = os.path.join(HERE, "data", "01302019.NASDAQ_ITCH50.prefix.gz")
RAW = os.path.join(HERE, "data", "01302019.NASDAQ_ITCH50.prefix.itch")
TARGET_COMPRESSED = int(os.environ.get("ITCH_TARGET_MB", "150")) * 1024 * 1024


def download() -> None:
    os.makedirs(os.path.dirname(GZ), exist_ok=True)
    socket.setdefaulttimeout(90)
    while True:
        have = os.path.getsize(GZ) if os.path.exists(GZ) else 0
        if have >= TARGET_COMPRESSED:
            print(f"[dl] complete: {have} compressed bytes", flush=True)
            return
        end = min(have + 8 * 1024 * 1024, TARGET_COMPRESSED) - 1
        req = urllib.request.Request(
            URL,
            headers={"User-Agent": "Mozilla/5.0", "Range": f"bytes={have}-{end}"},
        )
        try:
            t0 = time.time()
            with urllib.request.urlopen(req) as r:
                chunk = r.read()
            with open(GZ, "ab") as fh:
                fh.write(chunk)
            dt = time.time() - t0
            print(
                f"[dl] +{len(chunk)} -> {have + len(chunk)}/{TARGET_COMPRESSED} "
                f"({(have + len(chunk)) / TARGET_COMPRESSED:.1%}) {len(chunk)/dt/1e6:.2f} MB/s",
                flush=True,
            )
        except Exception as exc:  # network flakiness -> retry, we lose nothing
            print(f"[dl] retry after {type(exc).__name__}: {exc}", flush=True)
            time.sleep(3)


def decompress() -> None:
    """Decode the gzip prefix and truncate to the last complete ITCH message."""
    dec = zlib.decompressobj(31)
    out = bytearray()
    with open(GZ, "rb") as fh:
        while True:
            block = fh.read(1 << 22)
            if not block:
                break
            out += dec.decompress(block)
    # Walk the length-prefixed frame chain and cut at the last whole message.
    pos, n, count = 0, len(out), 0
    while pos + 2 <= n:
        length = (out[pos] << 8) | out[pos + 1]
        if pos + 2 + length > n:
            break
        pos += 2 + length
        count += 1
    with open(RAW, "wb") as fh:
        fh.write(memoryview(out)[:pos])
    print(f"[dl] raw={pos} bytes, {count} whole messages -> {RAW}", flush=True)


if __name__ == "__main__":
    if "--decompress-only" not in sys.argv:
        download()
    decompress()
