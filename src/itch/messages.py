"""NASDAQ TotalView-ITCH 5.0 message definitions.

Wire-format facts that drive every design decision in this package:

* **Big-endian.** ITCH is a network-order protocol. x86 is little-endian, so every
  multi-byte integer needs a byte swap. ``struct`` handles this with the ``>`` prefix;
  NumPy handles it with ``>u4``-style dtypes. Getting this wrong does not raise --
  it silently yields absurd numbers (a price of ``0x0000C350`` = $5.00 read
  little-endian becomes 1347420160), which is exactly why the round-trip tests in
  ``tests/test_decoder.py`` exist.

* **Fixed-length records, self-describing.** In the distributed BinaryFILE wrapper each
  message is preceded by a 2-byte big-endian length. The first payload byte is the
  message type, and *type alone determines length* -- the prefix is redundant. Both
  decoders validate the two against each other, so a desynchronised stream fails loudly
  instead of emitting garbage from wrong offsets (see FAILURES.md #1).

* **Prices are fixed-point integers with 4 implied decimal places.** ``$5.00`` is
  transmitted as ``50000``. We keep them as Python ``int`` / NumPy ``uint32`` end to
  end and divide by 10_000 only at the presentation boundary. Floats are not merely
  imprecise here, they are *wrong*: the order book keys price levels in a dict, and
  binary floating point cannot represent 0.0001 exactly, so two economically
  identical prices can hash to two different levels. Integer arithmetic is also exact
  for the aggregations we care about (notional = price * shares).

* **Timestamps are 6-byte (48-bit) nanoseconds since midnight, US/Eastern.** There is
  no 6-byte integer code in ``struct`` and no ``uint48`` in NumPy. Both decoders split
  the field into a 2-byte high half and a 4-byte low half and recombine as
  ``hi << 32 | lo`` -- scalar in the naive path, vectorised in the fast path.
"""

from __future__ import annotations

import struct as _struct
from typing import Final

import numpy as np

#: Fixed-point scale: prices carry four implied decimal places.
PRICE_SCALE: Final[int] = 10_000

#: Total on-wire payload length in bytes for every ITCH 5.0 message type,
#: *excluding* the 2-byte BinaryFILE length prefix. Types we do not decode into
#: structures still need their lengths so the framing scan can skip them correctly.
MESSAGE_LENGTHS: Final[dict[str, int]] = {
    "S": 12,  # System Event
    "R": 39,  # Stock Directory
    "H": 25,  # Stock Trading Action
    "Y": 20,  # Reg SHO Short Sale Price Test Restriction
    "L": 26,  # Market Participant Position
    "V": 35,  # MWCB Decline Level
    "W": 12,  # MWCB Status
    "K": 28,  # IPO Quoting Period Update
    "J": 35,  # LULD Auction Collar
    "h": 21,  # Operational Halt
    "A": 36,  # Add Order -- no MPID attribution
    "F": 40,  # Add Order -- with MPID attribution
    "E": 31,  # Order Executed
    "C": 36,  # Order Executed with Price
    "X": 23,  # Order Cancel
    "D": 19,  # Order Delete
    "U": 35,  # Order Replace
    "P": 44,  # Trade (non-cross) -- execution of a NON-displayable order
    "Q": 40,  # Cross Trade
    "B": 19,  # Broken Trade
    "I": 50,  # Net Order Imbalance Indicator (NOII)
    "N": 20,  # Retail Price Improvement Indicator
    "O": 48,  # Direct Listing with Capital Raise Price Discovery
}

#: The subset this project decodes into typed records. Everything else is counted and
#: skipped: decoding the NOII feed would not change the book reconstruction at all.
DECODED_TYPES: Final[frozenset[str]] = frozenset("SRAFECXDUP")

#: Every decoded message opens with the same 11-byte header:
#: ``type(1) stock_locate(2) tracking_number(2) timestamp(6)``.
HEADER_STRUCT: Final[str] = ">cHHHI"

#: ``struct`` format strings for the naive decoder, one per decoded type. The 6-byte
#: timestamp is spelled ``H`` + ``I`` because ``struct`` has no 48-bit code. The ``>``
#: prefix means big-endian *and* standard sizes with no padding -- both are required.
STRUCT_FORMATS: Final[dict[str, str]] = {
    # + event_code
    "S": ">cHHHIc",
    # + stock, market_category, financial_status, round_lot_size, round_lots_only,
    #   issue_classification, issue_subtype, authenticity, short_sale_threshold,
    #   ipo_flag, luld_tier, etp_flag, etp_leverage, inverse_indicator
    "R": ">cHHHI8sccIcc2scccccIc",
    # + order_ref, side, shares, stock, price
    "A": ">cHHHIQcI8sI",
    # + order_ref, side, shares, stock, price, attribution (MPID)
    "F": ">cHHHIQcI8sI4s",
    # + order_ref, executed_shares, match_number
    "E": ">cHHHIQIQ",
    # + order_ref, executed_shares, match_number, printable, execution_price
    "C": ">cHHHIQIQcI",
    # + order_ref, cancelled_shares
    "X": ">cHHHIQI",
    # + order_ref
    "D": ">cHHHIQ",
    # + original_order_ref, new_order_ref, shares, price
    "U": ">cHHHIQQII",
    # + order_ref, side, shares, stock, price, match_number
    "P": ">cHHHIQcI8sIQ",
}

#: NumPy structured dtypes for the vectorised decoder. Field order and widths mirror
#: ``STRUCT_FORMATS`` exactly; each dtype's ``itemsize`` must equal the spec length or
#: the reinterpret-cast in :mod:`itch.fast_decoder` reads every subsequent field from
#: the wrong offset. ``_validate_layouts()`` asserts this at import time.
#:
#: ``align=False`` is essential and is applied wherever these are turned into dtypes:
#: ITCH is packed with no padding, but NumPy's default would insert alignment padding
#: between, say, a ``u4`` and a following ``u8`` and silently corrupt the decode.
NUMPY_DTYPES: Final[dict[str, list[tuple[str, str]]]] = {
    "S": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("event_code", "S1"),
    ],
    "R": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("stock", "S8"),
        ("market_category", "S1"), ("financial_status", "S1"),
        ("round_lot_size", ">u4"), ("round_lots_only", "S1"),
        ("issue_classification", "S1"), ("issue_subtype", "S2"),
        ("authenticity", "S1"), ("short_sale_threshold", "S1"), ("ipo_flag", "S1"),
        ("luld_tier", "S1"), ("etp_flag", "S1"), ("etp_leverage", ">u4"),
        ("inverse_indicator", "S1"),
    ],
    "A": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("order_ref", ">u8"), ("side", "S1"),
        ("shares", ">u4"), ("stock", "S8"), ("price", ">u4"),
    ],
    "F": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("order_ref", ">u8"), ("side", "S1"),
        ("shares", ">u4"), ("stock", "S8"), ("price", ">u4"), ("attribution", "S4"),
    ],
    "E": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("order_ref", ">u8"),
        ("executed_shares", ">u4"), ("match_number", ">u8"),
    ],
    "C": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("order_ref", ">u8"),
        ("executed_shares", ">u4"), ("match_number", ">u8"), ("printable", "S1"),
        ("execution_price", ">u4"),
    ],
    "X": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("order_ref", ">u8"),
        ("cancelled_shares", ">u4"),
    ],
    "D": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("order_ref", ">u8"),
    ],
    "U": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("original_order_ref", ">u8"),
        ("new_order_ref", ">u8"), ("shares", ">u4"), ("price", ">u4"),
    ],
    "P": [
        ("msg_type", "S1"), ("stock_locate", ">u2"), ("tracking", ">u2"),
        ("ts_hi", ">u2"), ("ts_lo", ">u4"), ("order_ref", ">u8"), ("side", "S1"),
        ("shares", ">u4"), ("stock", "S8"), ("price", ">u4"), ("match_number", ">u8"),
    ],
}

BUY: Final[bytes] = b"B"
SELL: Final[bytes] = b"S"

SYSTEM_EVENT_NAMES: Final[dict[bytes, str]] = {
    b"O": "start_of_messages",
    b"S": "start_of_system_hours",
    b"Q": "start_of_market_hours",
    b"M": "end_of_market_hours",
    b"E": "end_of_system_hours",
    b"C": "end_of_messages",
}


def dtype_for(msg_type: str) -> np.dtype:
    """Return the packed (unaligned) NumPy dtype for one message type."""
    return np.dtype(NUMPY_DTYPES[msg_type], align=False)


def price_to_decimal(price_int: int) -> float:
    """Convert a fixed-point ITCH price to a float. Presentation boundary only.

    Never call this before arithmetic. See the module docstring for why float prices
    break dict-keyed price levels.
    """
    return price_int / PRICE_SCALE


def _validate_layouts() -> None:
    """Fail at import time if any layout disagrees with the published spec lengths.

    This is not defensive padding, it is the guard that turns a silent field
    misalignment -- the single nastiest class of binary-protocol bug, because it
    produces plausible-looking wrong numbers rather than an exception -- into an
    immediate, obvious failure.

    Concretely, it is what stands between this decoder and NumPy's default alignment
    behaviour: built with ``align=True`` the Add Order dtype is **48** bytes rather than
    the spec's 36, and every field after the first padded one would be read from the
    wrong offset without raising anything. See FAILURES.md #2.
    """
    for msg_type, fields in NUMPY_DTYPES.items():
        size = np.dtype(fields, align=False).itemsize
        expected = MESSAGE_LENGTHS[msg_type]
        if size != expected:
            raise AssertionError(
                f"NumPy dtype for message {msg_type!r} is {size} bytes, "
                f"spec says {expected}"
            )
    for msg_type, fmt in STRUCT_FORMATS.items():
        size = _struct.calcsize(fmt)
        expected = MESSAGE_LENGTHS[msg_type]
        if size != expected:
            raise AssertionError(
                f"struct format for message {msg_type!r} is {size} bytes, "
                f"spec says {expected}"
            )
    if not MESSAGE_LENGTHS.keys() >= DECODED_TYPES:
        raise AssertionError("DECODED_TYPES contains a type with no known length")


_validate_layouts()
