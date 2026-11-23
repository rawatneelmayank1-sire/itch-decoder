"""Naive reference decoder: one ``struct.unpack_from`` call per message.

This is the *correctness reference*. It is deliberately the most obvious thing that
could work -- walk the frame chain, unpack each record, build a typed object -- and it
is the oracle that :mod:`itch.fast_decoder` is tested against. Optimised code you
cannot check against a simple implementation is optimised code you cannot trust.

It is also the "before" half of the benchmark. See ``results/benchmarks.md``.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO, Final

from .messages import DECODED_TYPES, MESSAGE_LENGTHS, STRUCT_FORMATS

# Pre-compiling avoids re-parsing the format string on every message. struct caches
# internally, but the cache lookup itself is measurable at millions of messages.
_COMPILED: Final[dict[str, struct.Struct]] = {
    t: struct.Struct(f) for t, f in STRUCT_FORMATS.items()
}

# Message type -> payload length, keyed by the raw byte value so the hot loop can
# index with an int and skip a bytes->str decode per message.
_LEN_BY_BYTE: Final[dict[int, int]] = {
    ord(t): n for t, n in MESSAGE_LENGTHS.items()
}


class ITCHFormatError(ValueError):
    """Raised when the byte stream does not conform to ITCH 5.0 framing."""


@dataclass(slots=True, frozen=True)
class Message:
    """One decoded ITCH message.

    ``slots=True`` matters: at tens of millions of messages a per-instance ``__dict__``
    costs both memory and attribute-access time. ``frozen=True`` documents that a
    decoded message is an immutable record of what the exchange said.

    ``fields`` holds the type-specific tail exactly as ``struct`` produced it. Keeping
    it as a tuple rather than exploding it into named attributes is what lets one
    dataclass serve all ten message types without a class hierarchy; the order book
    indexes it positionally through the small accessor properties below.
    """

    msg_type: str
    stock_locate: int
    tracking: int
    timestamp: int
    """Nanoseconds since midnight US/Eastern, reassembled from the 48-bit field."""
    fields: tuple[object, ...]

    # -- Accessors for the fields the order book needs. Positional indices follow
    # -- STRUCT_FORMATS; the round-trip tests pin them.
    @property
    def order_ref(self) -> int:
        """Order reference number. Valid for A, F, E, C, X, D, P."""
        return self.fields[0]  # type: ignore[return-value]

    @property
    def side(self) -> bytes:
        """b'B' or b'S'. Valid for A, F, P only."""
        return self.fields[1]  # type: ignore[return-value]

    @property
    def shares(self) -> int:
        """Displayed size. Valid for A, F, P."""
        return self.fields[2]  # type: ignore[return-value]

    @property
    def stock(self) -> bytes:
        """8-byte space-padded ticker. Valid for A, F, P, R."""
        return self.fields[3]  # type: ignore[return-value]

    @property
    def price(self) -> int:
        """Fixed-point price, 4 implied decimals. Valid for A, F, P."""
        return self.fields[4]  # type: ignore[return-value]


def iter_frames(
    data: bytes | memoryview, *, strict_types: bool = False
) -> Iterator[tuple[int, int, int]]:
    """Walk the BinaryFILE frame chain, yielding ``(offset, type_byte, length)``.

    ``offset`` points at the *payload* (past the 2-byte length prefix).

    The scan is inherently sequential: a record's length is only knowable once you are
    positioned at its start, so the offsets form a chain, not an arithmetic sequence.
    This is the one part of decoding that cannot be vectorised away, and it is why the
    fast decoder still pays a Python loop here (see DESIGN.md).

    A trailing partial message -- which is exactly what a byte-range prefix of a real
    file ends with -- terminates the iteration cleanly rather than raising.

    **Unknown message types are skipped, not fatal.** There is a deliberate asymmetry
    here, and it comes from the fact that the length prefix is carried *independently*
    of the type table:

    * An **unknown type** is recoverable. The prefix already told us how many bytes to
      skip, so we can step over the record and carry on with framing intact. This is
      not hypothetical tolerance: ITCH gains message types over time (``O``, Direct
      Listing with Capital Raise, arrived in the April 2023 revision), and a decoder
      that dies on the first type it has not heard of cannot read next year's file.
    * A **length that disagrees** with a known type is *not* recoverable. It means
      framing has been lost and every subsequent offset is garbage, so it raises.

    Pass ``strict_types=True`` to reject unknown types as well -- appropriate when you
    are validating a capture rather than consuming one.
    """
    view = memoryview(data)
    pos = 0
    end = len(view)
    while pos + 2 <= end:
        length = (view[pos] << 8) | view[pos + 1]
        if length == 0:
            raise ITCHFormatError(f"zero-length message frame at byte {pos}")
        payload = pos + 2
        if payload >= end:
            return  # not even the type byte is present; genuinely truncated

        # ORDER MATTERS. Validate the type/length agreement BEFORE the fit check.
        # A corrupt length prefix on the last frame makes the frame appear to overrun
        # the buffer, which is indistinguishable from an honest truncated tail -- so
        # checking "does it fit" first silently swallows real corruption. The type byte
        # is readable either way, so validate against it first. (FAILURES.md #1.)
        type_byte = view[payload]
        expected = _LEN_BY_BYTE.get(type_byte)
        if expected is None:
            if strict_types:
                raise ITCHFormatError(
                    f"unknown message type {chr(type_byte)!r} at byte {payload}"
                )
            if payload + length > end:
                return
            yield payload, type_byte, length
            pos = payload + length
            continue
        if expected != length:
            # The length prefix and the type byte are redundant encodings of the same
            # fact. When they disagree we are desynchronised from the stream and every
            # subsequent offset is garbage -- fail loudly rather than emit nonsense.
            raise ITCHFormatError(
                f"framing desync at byte {pos}: prefix says {length} bytes but "
                f"message type {chr(type_byte)!r} is {expected} bytes"
            )
        if payload + length > end:
            return  # truncated tail; caller keeps everything decoded so far
        yield payload, type_byte, length
        pos = payload + length


def decode(
    data: bytes | memoryview, *, limit: int | None = None, strict_types: bool = False
) -> list[Message]:
    """Decode a buffer into a list of :class:`Message`, one ``unpack_from`` per record.

    Undecoded message types (H, Y, L, Q, ...) are skipped, not represented. Use
    :func:`count_message_types` if you want the full census.
    """
    out: list[Message] = []
    append = out.append
    compiled = _COMPILED
    decoded = DECODED_TYPES
    for offset, type_byte, _length in iter_frames(data, strict_types=strict_types):
        msg_type = chr(type_byte)
        if msg_type not in decoded:
            continue
        unpacked = compiled[msg_type].unpack_from(data, offset)
        # unpacked = (type, stock_locate, tracking, ts_hi, ts_lo, *type_specific)
        append(
            Message(
                msg_type=msg_type,
                stock_locate=unpacked[1],
                tracking=unpacked[2],
                # 48-bit timestamp: no struct code exists, so recombine the halves.
                timestamp=(unpacked[3] << 32) | unpacked[4],
                fields=unpacked[5:],
            )
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def iter_decode(
    data: bytes | memoryview, *, limit: int | None = None
) -> Iterator[Message]:
    """Streaming form of :func:`decode`; constant memory over the message list."""
    compiled = _COMPILED
    decoded = DECODED_TYPES
    emitted = 0
    for offset, type_byte, _length in iter_frames(data):
        msg_type = chr(type_byte)
        if msg_type not in decoded:
            continue
        unpacked = compiled[msg_type].unpack_from(data, offset)
        yield Message(
            msg_type=msg_type,
            stock_locate=unpacked[1],
            tracking=unpacked[2],
            timestamp=(unpacked[3] << 32) | unpacked[4],
            fields=unpacked[5:],
        )
        emitted += 1
        if limit is not None and emitted >= limit:
            return


def count_message_types(data: bytes | memoryview) -> dict[str, int]:
    """Census of every message type present, including ones we do not decode.

    Cheap: touches only the framing bytes, never unpacks a payload.
    """
    counts: dict[str, int] = {}
    for _offset, type_byte, _length in iter_frames(data):
        key = chr(type_byte)
        counts[key] = counts.get(key, 0) + 1
    return counts


def read_file(path: str, *, max_bytes: int | None = None) -> bytes:
    """Read an ITCH file (or a prefix of one) into memory.

    Truncates at a whole-message boundary so a size-capped read never leaves a partial
    record for the caller to trip over.
    """
    with open(path, "rb") as handle:
        return _read_handle(handle, max_bytes)


def _read_handle(handle: BinaryIO, max_bytes: int | None) -> bytes:
    data = handle.read() if max_bytes is None else handle.read(max_bytes)
    if max_bytes is None:
        return data
    # Re-walk to find the last complete frame so the cap lands on a boundary.
    pos = 0
    end = len(data)
    while pos + 2 <= end:
        length = (data[pos] << 8) | data[pos + 1]
        if pos + 2 + length > end:
            break
        pos += 2 + length
    return data[:pos]
