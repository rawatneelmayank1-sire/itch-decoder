"""Vectorised decoder: batch records of the same type through NumPy structured dtypes.

The optimisation in one sentence: **stop doing per-message work in Python**.

The naive decoder pays, per message, one ``struct.unpack_from`` call, one tuple
allocation and one dataclass construction. At tens of millions of messages that is
tens of millions of Python-level operations, and CPython's per-operation overhead --
not the byte swapping -- dominates the profile.

This decoder splits decoding into two phases:

1. **Framing scan** (still a Python loop, unavoidably). A record's length is only
   knowable once you are positioned at its start, so message offsets form a *chain*,
   not an arithmetic sequence, and cannot be computed with a ``cumsum``. The loop is
   stripped to the minimum: two byte reads, a shift, an add, an append. No unpacking,
   no object construction.

2. **Columnar extraction** (fully vectorised). Group offsets by message type, gather
   each group's bytes into a contiguous ``(n, record_length)`` ``uint8`` block, then
   *reinterpret* that block as a structured array. NumPy does the big-endian
   conversion field-wise in C over the whole batch. Cost per message drops to a few
   memory accesses.

The residual Python loop is Amdahl's serial fraction for this design; ``itch bench
--profile`` reports what share of runtime it actually is, and DESIGN.md covers how
you would remove it.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from .decoder import ITCHFormatError
from .messages import DECODED_TYPES, MESSAGE_LENGTHS, dtype_for

#: Lookup table: message type byte value -> payload length. Index 0 means "unknown",
#: which lets the scan validate types with an array index instead of a dict lookup.
_LEN_TABLE: Final[np.ndarray] = np.zeros(256, dtype=np.int64)
for _t, _n in MESSAGE_LENGTHS.items():
    _LEN_TABLE[ord(_t)] = _n

#: Gather chunk size. The index array for a gather is ``chunk * record_length`` int64s,
#: so chunking caps that temporary at a few tens of MB instead of letting it scale with
#: the file. Found by measurement -- see results/benchmarks.md; larger chunks stopped
#: helping once the index array exceeded L3.
_GATHER_CHUNK: Final[int] = 1 << 16


@dataclass(slots=True)
class DecodedBatch:
    """Columnar decode result.

    ``arrays[t]`` is a NumPy structured array of every message of type ``t``, in stream
    order. ``sequence[t]`` gives each of those messages its position in the original
    stream, so the per-type arrays can be re-interleaved for stateful replay (the order
    book needs strict stream order; see :func:`book_events`).
    """

    arrays: dict[str, np.ndarray] = field(default_factory=dict)
    sequence: dict[str, np.ndarray] = field(default_factory=dict)
    type_counts: dict[str, int] = field(default_factory=dict)
    total_messages: int = 0
    bytes_consumed: int = 0

    def timestamps(self, msg_type: str) -> np.ndarray:
        """Reassemble the 48-bit timestamps for one message type, vectorised.

        NumPy has no ``uint48``, so the field is stored split. Widening ``ts_hi`` to
        ``uint64`` *before* the shift is mandatory -- shifting a ``uint16`` left by 32
        is undefined-ish and in practice gives zero.
        """
        arr = self.arrays[msg_type]
        return (arr["ts_hi"].astype(np.uint64) << np.uint64(32)) | arr["ts_lo"].astype(
            np.uint64
        )


def scan_frames(
    data: bytes | memoryview, *, strict_types: bool = False
) -> tuple[np.ndarray, np.ndarray, int]:
    """Walk the frame chain and return ``(payload_offsets, type_bytes, bytes_consumed)``.

    This is the serial phase. Everything downstream is vectorised.

    Uses ``array('q')`` rather than a list: it stores raw int64s, so the handoff to
    NumPy is a zero-copy ``frombuffer`` instead of a per-element unboxing pass.

    A trailing partial message ends the scan cleanly -- which is what a byte-range
    prefix of a real exchange file always ends with.
    """
    buf = data if isinstance(data, bytes) else bytes(data)
    offsets = array("q")
    append = offsets.append
    pos = 0
    end = len(buf)
    while pos + 2 <= end:
        length = (buf[pos] << 8) | buf[pos + 1]
        payload = pos + 2
        if payload + length > end:
            break
        append(payload)
        pos = payload + length

    payload_offsets = np.frombuffer(offsets, dtype=np.int64)
    blob = np.frombuffer(buf, dtype=np.uint8)

    # Validate whatever is left over after the last complete frame. The hot loop above
    # stops as soon as a frame does not fit, which makes a *corrupt* length prefix look
    # exactly like an honest truncated tail. Checking the leftover here keeps the loop
    # lean while still catching the corruption. (FAILURES.md #1.)
    _validate_tail(blob, pos, end)

    if payload_offsets.size == 0:
        return payload_offsets, np.empty(0, dtype=np.uint8), 0

    type_bytes = blob[payload_offsets]

    # Vectorised validation: the length prefix and the type byte are redundant
    # encodings of the same fact. If they ever disagree we are desynchronised and
    # every later offset is garbage, so check the whole file at once rather than
    # per-message in the hot loop.
    expected = _LEN_TABLE[type_bytes]
    actual = np.empty_like(expected)
    actual[:-1] = np.diff(payload_offsets) - 2
    # The final complete frame's span IS observable: the scan consumed up to `pos`.
    actual[-1] = pos - payload_offsets[-1]

    # Unknown types (expected == 0) are skipped, not fatal: the length prefix is carried
    # independently of the type table, so we already know how far to step and framing
    # stays intact. ITCH gains message types over time. A *disagreeing* length on a
    # known type is a different matter and is not recoverable. See decoder.iter_frames.
    known = expected != 0
    if strict_types and not known.all():
        bad = int(np.flatnonzero(~known)[0])
        raise ITCHFormatError(
            f"unknown message type {chr(int(type_bytes[bad]))!r} at byte "
            f"{int(payload_offsets[bad])}"
        )
    mismatched = known & (actual != expected)
    if mismatched.any():
        bad = int(np.flatnonzero(mismatched)[0])
        raise ITCHFormatError(
            f"framing desync at byte {int(payload_offsets[bad])}: prefix implies "
            f"{int(actual[bad])} bytes but type "
            f"{chr(int(type_bytes[bad]))!r} is {int(expected[bad])} bytes"
        )
    return payload_offsets, type_bytes, pos


def _validate_tail(blob: np.ndarray, pos: int, end: int) -> None:
    """Check the bytes after the last complete frame are an honest truncated message.

    An honest tail carries a length prefix that agrees with its type byte; it is simply
    cut short. Anything else means the stream desynchronised and every offset we just
    produced is suspect.
    """
    if pos + 2 > end:
        return  # fewer than two bytes left: nothing to validate
    length = (int(blob[pos]) << 8) | int(blob[pos + 1])
    payload = pos + 2
    if payload >= end:
        return  # type byte not present, so nothing to check it against
    type_byte = int(blob[payload])
    expected = int(_LEN_TABLE[type_byte])
    if expected == 0:
        return  # unknown type in the trailing partial frame: skippable, not fatal
    if expected != length:
        raise ITCHFormatError(
            f"framing desync at byte {pos}: prefix says {length} bytes but "
            f"message type {chr(type_byte)!r} is {expected} bytes"
        )


def _gather_records(
    blob: np.ndarray, offsets: np.ndarray, record_length: int
) -> np.ndarray:
    """Gather ``len(offsets)`` records of ``record_length`` bytes into a dense block.

    The records of one type are scattered through the file, so this is a genuine
    gather, not a slice. Chunked so the int64 index matrix stays cache-resident
    instead of growing with the file.
    """
    n = offsets.size
    out = np.empty((n, record_length), dtype=np.uint8)
    column = np.arange(record_length, dtype=np.int64)
    for start in range(0, n, _GATHER_CHUNK):
        stop = min(start + _GATHER_CHUNK, n)
        idx = offsets[start:stop, None] + column[None, :]
        np.take(blob, idx, out=out[start:stop])
    return out


def decode_columnar(
    data: bytes | memoryview,
    *,
    types: frozenset[str] | None = None,
    strict_types: bool = False,
) -> DecodedBatch:
    """Decode a buffer into per-type NumPy structured arrays.

    ``types`` restricts which message types are materialised. Skipping types you do not
    need is nearly free here -- the scan already counted them -- and it is the single
    biggest lever when you only care about, say, order events.
    """
    wanted = DECODED_TYPES if types is None else types
    payload_offsets, type_bytes, consumed = scan_frames(data, strict_types=strict_types)
    batch = DecodedBatch(
        total_messages=int(payload_offsets.size), bytes_consumed=consumed
    )
    if payload_offsets.size == 0:
        return batch

    # Full census, vectorised: one pass over the type column, no Python loop.
    present, counts = np.unique(type_bytes, return_counts=True)
    batch.type_counts = {
        chr(int(t)): int(c) for t, c in zip(present, counts, strict=True)
    }

    buf = data if isinstance(data, bytes) else bytes(data)
    blob = np.frombuffer(buf, dtype=np.uint8)
    for msg_type in sorted(wanted):
        code = ord(msg_type)
        mask = type_bytes == code
        n = int(mask.sum())
        if n == 0:
            continue
        selected = payload_offsets[mask]
        record_length = MESSAGE_LENGTHS[msg_type]
        rows = _gather_records(blob, selected, record_length)
        # The reinterpret-cast. rows is (n, record_length) C-contiguous uint8, and the
        # dtype's itemsize is exactly record_length (asserted at import in messages.py),
        # so this view is a pure relabelling -- no copy, no conversion pass.
        batch.arrays[msg_type] = rows.view(dtype_for(msg_type)).reshape(-1)
        batch.sequence[msg_type] = np.flatnonzero(mask)
    return batch


#: Unified event record used to replay a stream through the order book. One flat dtype
#: for all book-affecting message types beats a Python-object-per-message stream by an
#: order of magnitude, and beats a dict-of-arrays because the replay loop is row-wise.
BOOK_EVENT_DTYPE: Final[np.dtype] = np.dtype(
    [
        ("seq", "<i8"),
        ("msg_type", "S1"),
        ("timestamp", "<u8"),
        ("stock_locate", "<u4"),
        ("order_ref", "<u8"),
        ("new_order_ref", "<u8"),
        ("side", "S1"),
        ("shares", "<u4"),
        ("price", "<u4"),
        ("match_number", "<u8"),
        ("printable", "S1"),
    ],
    align=False,
)

#: Message types that mutate or report on the displayed book.
BOOK_TYPES: Final[tuple[str, ...]] = ("A", "F", "E", "C", "X", "D", "U", "P")


def book_events(
    batch: DecodedBatch, *, stock_locate: int | None = None
) -> np.ndarray:
    """Flatten a :class:`DecodedBatch` into stream-ordered book events.

    Filtering by ``stock_locate`` here rather than inside the replay loop is the
    difference between holding the whole day's order flow in memory and holding one
    symbol's. Locate codes are assigned by the Stock Directory (R) messages.
    """
    pieces: list[np.ndarray] = []
    for msg_type in BOOK_TYPES:
        arr = batch.arrays.get(msg_type)
        if arr is None or arr.size == 0:
            continue
        seq = batch.sequence[msg_type]
        if stock_locate is not None:
            keep = arr["stock_locate"] == stock_locate
            arr = arr[keep]
            seq = seq[keep]
            if arr.size == 0:
                continue
        out = np.zeros(arr.size, dtype=BOOK_EVENT_DTYPE)
        out["seq"] = seq
        out["msg_type"] = msg_type.encode()
        out["timestamp"] = (arr["ts_hi"].astype(np.uint64) << np.uint64(32)) | arr[
            "ts_lo"
        ].astype(np.uint64)
        out["stock_locate"] = arr["stock_locate"]
        if msg_type == "U":
            out["order_ref"] = arr["original_order_ref"]
            out["new_order_ref"] = arr["new_order_ref"]
            out["shares"] = arr["shares"]
            out["price"] = arr["price"]
        else:
            out["order_ref"] = arr["order_ref"]
            if msg_type in ("A", "F", "P"):
                out["side"] = arr["side"]
                out["shares"] = arr["shares"]
                out["price"] = arr["price"]
            if msg_type in ("E", "C"):
                out["shares"] = arr["executed_shares"]
                out["match_number"] = arr["match_number"]
            if msg_type == "C":
                out["price"] = arr["execution_price"]
                out["printable"] = arr["printable"]
            if msg_type == "X":
                out["shares"] = arr["cancelled_shares"]
            if msg_type == "P":
                out["match_number"] = arr["match_number"]
        pieces.append(out)
    if not pieces:
        return np.empty(0, dtype=BOOK_EVENT_DTYPE)
    merged = np.concatenate(pieces)
    # Stream order is non-negotiable for a stateful replay: a cancel that arrives
    # before its add is a different (and wrong) book.
    merged.sort(order="seq", kind="stable")
    return merged


def symbol_directory(batch: DecodedBatch) -> dict[str, int]:
    """Map ticker -> stock_locate from the Stock Directory (R) messages."""
    arr = batch.arrays.get("R")
    if arr is None:
        return {}
    return {
        s.decode("ascii").strip(): int(loc)
        for s, loc in zip(arr["stock"], arr["stock_locate"], strict=True)
    }
