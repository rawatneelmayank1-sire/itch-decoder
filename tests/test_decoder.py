"""Decoder tests: round-trip, spec conformance, endianness, and cross-implementation.

The round trip matters because the encoder in :mod:`itch.generator` is written from the
published field tables, independently of ``STRUCT_FORMATS``. If the decoder's offsets
were derived from the encoder these tests would be tautological; they are not.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from itch.decoder import ITCHFormatError, count_message_types, decode, iter_frames
from itch.fast_decoder import book_events, decode_columnar, scan_frames
from itch.generator import SyntheticStream, encode
from itch.messages import MESSAGE_LENGTHS, NUMPY_DTYPES, PRICE_SCALE, STRUCT_FORMATS

# --------------------------------------------------------------------- layout


def test_every_dtype_matches_the_spec_length() -> None:
    """A dtype whose itemsize drifts from the spec silently misaligns every field."""
    for msg_type, fields in NUMPY_DTYPES.items():
        assert np.dtype(fields, align=False).itemsize == MESSAGE_LENGTHS[msg_type]


def test_every_struct_format_matches_the_spec_length() -> None:
    for msg_type, fmt in STRUCT_FORMATS.items():
        assert struct.calcsize(fmt) == MESSAGE_LENGTHS[msg_type]


def test_numpy_and_struct_layouts_agree() -> None:
    """The two decoders must describe the same bytes, or one of them is lying."""
    for msg_type in STRUCT_FORMATS:
        assert struct.calcsize(STRUCT_FORMATS[msg_type]) == np.dtype(
            NUMPY_DTYPES[msg_type], align=False
        ).itemsize


# ----------------------------------------------------------------- round trip


ROUND_TRIP_CASES = [
    ("S", {"timestamp": 34_200_000_000_000, "event_code": "Q"}),
    ("R", {"stock_locate": 7, "timestamp": 1, "stock": "AAPL", "round_lot_size": 100}),
    (
        "A",
        {
            "stock_locate": 14, "timestamp": 34_200_123_456_789, "order_ref": 2**48 + 7,
            "side": "B", "shares": 1_000_000, "stock": "MSFT", "price": 1_234_5678,
        },
    ),
    (
        "F",
        {
            "stock_locate": 3, "timestamp": 999, "order_ref": 42, "side": "S",
            "shares": 100, "stock": "TSLA", "price": 50_000, "attribution": "NSDQ",
        },
    ),
    ("E", {"stock_locate": 1, "timestamp": 5, "order_ref": 99, "executed_shares": 300,
           "match_number": 2**40}),
    ("C", {"stock_locate": 1, "timestamp": 5, "order_ref": 99, "executed_shares": 300,
           "match_number": 7, "printable": "N", "execution_price": 123_456}),
    ("X", {"stock_locate": 2, "timestamp": 6, "order_ref": 5, "cancelled_shares": 250}),
    ("D", {"stock_locate": 2, "timestamp": 7, "order_ref": 2**63}),
    ("U", {"stock_locate": 2, "timestamp": 8, "original_order_ref": 11,
           "new_order_ref": 12, "shares": 400, "price": 987_654}),
    ("P", {"stock_locate": 9, "timestamp": 10, "order_ref": 77, "side": "B",
           "shares": 500, "stock": "NVDA", "price": 1_500_000, "match_number": 3}),
]


@pytest.mark.parametrize(("msg_type", "fields"), ROUND_TRIP_CASES, ids=[c[0] for c in ROUND_TRIP_CASES])
def test_round_trip_naive_decoder(msg_type: str, fields: dict) -> None:
    """encode -> decode -> compare, for every decoded message type."""
    raw = encode(msg_type, **fields)
    assert len(raw) == MESSAGE_LENGTHS[msg_type] + 2  # + 2-byte length prefix

    (message,) = decode(raw)
    assert message.msg_type == msg_type
    assert message.stock_locate == fields.get("stock_locate", 0)
    assert message.timestamp == fields.get("timestamp", 0)


@pytest.mark.parametrize(("msg_type", "fields"), ROUND_TRIP_CASES, ids=[c[0] for c in ROUND_TRIP_CASES])
def test_round_trip_vectorised_decoder(msg_type: str, fields: dict) -> None:
    raw = encode(msg_type, **fields)
    batch = decode_columnar(raw)
    arr = batch.arrays[msg_type]
    assert len(arr) == 1
    assert int(arr["stock_locate"][0]) == fields.get("stock_locate", 0)
    assert int(batch.timestamps(msg_type)[0]) == fields.get("timestamp", 0)


def test_round_trip_preserves_every_add_order_field() -> None:
    """Full field-level round trip on the message type that uses every field kind."""
    fields = {
        "stock_locate": 4242, "tracking": 7, "timestamp": 34_200_999_888_777,
        "order_ref": 18_446_744_073_709_551_615,  # 2**64 - 1, the max u64
        "side": "S", "shares": 4_294_967_295,  # 2**32 - 1, the max u32
        "stock": "BRK.A", "price": 4_294_967_295,
    }
    raw = encode("A", **fields)
    (message,) = decode(raw)
    assert message.stock_locate == fields["stock_locate"]
    assert message.tracking == fields["tracking"]
    assert message.timestamp == fields["timestamp"]
    assert message.order_ref == fields["order_ref"]
    assert message.side == b"S"
    assert message.shares == fields["shares"]
    assert message.stock == b"BRK.A   "  # space-padded to 8, per spec
    assert message.price == fields["price"]

    batch = decode_columnar(raw)
    row = batch.arrays["A"][0]
    assert int(row["order_ref"]) == fields["order_ref"]
    assert int(row["price"]) == fields["price"]
    assert bytes(row["stock"]) == b"BRK.A   "


def test_timestamp_uses_all_48_bits() -> None:
    """The 48-bit timestamp is split hi/lo in both decoders; check the seam.

    2**48 - 1 ns is ~78 hours, comfortably past any trading day, so a decoder that
    silently truncated to 32 bits would still look plausible on real data. Hence an
    explicit maximum-value test.
    """
    biggest = 2**48 - 1
    raw = encode("D", timestamp=biggest, order_ref=1)
    (message,) = decode(raw)
    assert message.timestamp == biggest
    assert int(decode_columnar(raw).timestamps("D")[0]) == biggest


def test_decoders_are_big_endian() -> None:
    """Pin the byte order explicitly rather than trusting it to be right by luck.

    $5.00 is 50000 == 0x0000C350. Read little-endian that is 1,347,420,160.
    """
    raw = encode("A", order_ref=1, side="B", shares=100, stock="X", price=50_000)
    assert b"\x00\x00\xc3\x50" in raw
    (message,) = decode(raw)
    assert message.price == 50_000
    assert message.price / PRICE_SCALE == 5.00


def test_prices_stay_integers() -> None:
    """No float ever touches a price on the decode path."""
    raw = encode("A", order_ref=1, side="B", shares=100, stock="X", price=1_000_1)
    (message,) = decode(raw)
    assert isinstance(message.price, int)
    assert not isinstance(message.price, float)
    row = decode_columnar(raw).arrays["A"][0]
    assert np.issubdtype(row["price"].dtype, np.integer)


# --------------------------------------------------------------------- framing


def test_frame_scan_agrees_between_implementations() -> None:
    data = SyntheticStream(seed=7).generate(5_000)
    naive_offsets = [offset for offset, _t, _n in iter_frames(data)]
    fast_offsets, _types, _consumed = scan_frames(data)
    assert naive_offsets == fast_offsets.tolist()


def test_truncated_trailing_message_is_ignored_not_fatal() -> None:
    """A byte-range prefix of a real file always ends mid-message."""
    data = SyntheticStream(seed=11).generate(500)
    full = len(decode(data))
    truncated = data[:-5]  # cut the last message short, mid-payload
    assert len(decode(truncated)) == full - 1
    assert decode_columnar(truncated).total_messages == full - 1


def test_length_prefix_disagreeing_with_type_is_rejected() -> None:
    """The prefix and the type byte redundantly encode length. Disagreement = desync."""
    raw = bytearray(encode("D", order_ref=1, timestamp=1))
    raw[1] = 20  # claim 20 bytes; type 'D' is 19
    with pytest.raises(ITCHFormatError, match="desync"):
        decode(bytes(raw))
    with pytest.raises(ITCHFormatError, match="desync"):
        decode_columnar(bytes(raw))


def test_unknown_message_type_is_skipped_by_default() -> None:
    """Unknown types are recoverable; framing survives them.

    The length prefix is carried independently of the type table, so an unrecognised
    type can be stepped over with framing intact. This matters in practice: ITCH gains
    message types over time (``O`` arrived in the April 2023 revision), and a decoder
    that dies on the first unfamiliar type cannot read a newer file.
    """
    good = encode("A", stock_locate=1, order_ref=7, side="B", shares=100,
                  stock="AAPL", price=1_000_000)
    unknown = bytearray(encode("D", order_ref=1))
    unknown[2] = ord("z")  # not an ITCH 5.0 type; length prefix still says 19
    stream = good + bytes(unknown) + good

    messages = decode(stream)
    assert [m.msg_type for m in messages] == ["A", "A"], "framing must survive the gap"
    assert messages[1].order_ref == 7  # the message *after* the unknown type is intact

    batch = decode_columnar(stream)
    assert batch.total_messages == 3
    assert batch.type_counts["z"] == 1
    assert len(batch.arrays["A"]) == 2


def test_unknown_message_type_is_rejected_in_strict_mode() -> None:
    """Validating a capture is a different job from consuming one."""
    raw = bytearray(encode("D", order_ref=1))
    raw[2] = ord("z")
    with pytest.raises(ITCHFormatError, match="unknown message type"):
        decode(bytes(raw), strict_types=True)
    with pytest.raises(ITCHFormatError, match="unknown message type"):
        decode_columnar(bytes(raw), strict_types=True)


def test_zero_length_frame_is_rejected() -> None:
    with pytest.raises(ITCHFormatError, match="zero-length"):
        decode(b"\x00\x00")


def test_empty_buffer_decodes_to_nothing() -> None:
    assert decode(b"") == []
    assert decode_columnar(b"").total_messages == 0
    assert book_events(decode_columnar(b"")).size == 0


# ------------------------------------------------- naive vs vectorised, in bulk


def test_both_decoders_agree_on_a_full_synthetic_stream() -> None:
    """The real cross-check: every Add Order field, every message, both paths."""
    data = SyntheticStream(seed=99).generate(20_000)
    naive_adds = [m for m in decode(data) if m.msg_type == "A"]
    batch = decode_columnar(data)
    fast = batch.arrays["A"]
    stamps = batch.timestamps("A")
    assert len(naive_adds) == len(fast)
    for i, message in enumerate(naive_adds):
        assert message.stock_locate == int(fast["stock_locate"][i])
        assert message.timestamp == int(stamps[i])
        assert message.order_ref == int(fast["order_ref"][i])
        assert message.side == bytes(fast["side"][i])
        assert message.shares == int(fast["shares"][i])
        assert message.stock == bytes(fast["stock"][i])
        assert message.price == int(fast["price"][i])


def test_message_census_matches_between_implementations() -> None:
    data = SyntheticStream(seed=123).generate(10_000)
    assert count_message_types(data) == decode_columnar(data).type_counts


def test_book_events_are_in_stream_order() -> None:
    """A stateful replay has exactly one correct interleaving."""
    data = SyntheticStream(seed=5).generate(10_000)
    events = book_events(decode_columnar(data))
    seq = events["seq"]
    assert np.all(np.diff(seq) > 0)


def test_book_events_filter_by_symbol() -> None:
    data = SyntheticStream(seed=6).generate(10_000)
    batch = decode_columnar(data)
    everything = book_events(batch)
    just_one = book_events(batch, stock_locate=2)
    assert just_one.size < everything.size
    assert set(just_one["stock_locate"].tolist()) == {2}


def test_replace_message_carries_both_references() -> None:
    """U is the only message with two order references; make sure both survive."""
    raw = encode("U", stock_locate=1, original_order_ref=111, new_order_ref=222,
                 shares=300, price=400)
    events = book_events(decode_columnar(raw))
    assert int(events["order_ref"][0]) == 111
    assert int(events["new_order_ref"][0]) == 222
