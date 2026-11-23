#!/usr/bin/env python3
"""
Build byte-exact ITCH 5.0 test fixtures BY HAND from docs/itch-5.0-spec.md.

INDEPENDENCE GUARANTEE — read this before changing anything here.

These fixtures are the ground truth the decoder in src/ is tested against. They must therefore
NOT be produced by, derived from, or validated against that decoder. Accordingly:

  * No import from `itch`, `src/`, or any parsing library. This file's only imports are
    stdlib json/pathlib.
  * Field VALUES are declared first, as Python literals, in FIXTURES below. The bytes are then
    assembled FROM those values, field by field, each written at its literal declared offset into
    a zero-filled buffer of the declared total length.
  * The assembler deliberately does NOT use `struct` with the spec's format strings. It writes
    each field with `int.to_bytes(n, "big")` or ASCII+space-padding, independently, so that a
    mistake in a spec format string cannot propagate into a fixture and silently "confirm" itself.
  * The JSON expectations are written from the same declared literals, not by decoding the bytes.

Self-checks enforced at build time (any failure aborts the build):
  * every byte of every message is covered by exactly one field - no gaps, no overlaps;
  * fields are contiguous and start at offset 0;
  * the assembled length equals the total length declared for that type;
  * that declared length equals the length recorded in docs/itch-5.0-spec.md Part 5;
  * every alpha value fits its field; every integer fits its width;
  * every Price(4) is within the spec maximum of 200000.0000 (raw 0x77359400).

Output, per message type, in this directory:
  <T>_<slug>.itch   one message, FRAMED - 2-byte big-endian length prefix then the body
  <T>_<slug>.json   expected decode: per-field offset, length, type, raw value, decoded value

Plus malformed/ with deliberately broken inputs and their expected failure modes.

Run:  python data/fixtures/build_fixtures.py
Idempotent - rewrites identical bytes every time.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
MALFORMED = HERE / "malformed"

# Message lengths, transcribed from docs/itch-5.0-spec.md Part 5 (which was transcribed from the
# official Nasdaq PDF). Used only as an independent cross-check on the tables below.
SPEC_LENGTHS = {
    "S": 12, "R": 39, "H": 25, "Y": 20, "L": 26, "V": 35, "W": 12, "K": 28, "J": 35,
    "h": 21, "A": 36, "F": 40, "E": 31, "C": 36, "X": 23, "D": 19, "U": 35, "P": 44,
    "Q": 40, "B": 19, "I": 50, "N": 20, "O": 48,
}

PRICE4_SCALE = 10_000
PRICE8_SCALE = 100_000_000
PRICE4_MAX_RAW = 0x77359400  # 200,000.0000

# A timestamp whose high 16 bits are non-zero, so that a decoder wrongly reading only 4 bytes
# produces a visibly wrong answer instead of a plausible one.
# 34,200,000,000,123 ns = 09:30:00.000000123 ET.  high16 = 0x1F1A, low32 = 0x3B9AC9FB... (checked).
TS = 34_200_000_000_123

# Field kinds:
#   "type"   1-byte message type character
#   "alpha"  fixed-width ASCII, LEFT justified, RIGHT padded with 0x20 spaces
#   "uint"   unsigned big-endian integer of the field width
#   "ts"     6-byte unsigned big-endian nanoseconds since midnight
#   "price4" 4-byte unsigned big-endian, 4 implied decimals
#   "price8" 8-byte unsigned big-endian, 8 implied decimals
#
# Each entry: (name, offset, length, kind, value)

FIXTURES = {
    # ---------------------------------------------------------------- system / admin
    "S": ("system_event_start_of_market_hours", [
        ("msg_type", 0, 1, "type", "S"),
        ("stock_locate", 1, 2, "uint", 0),
        ("tracking_number", 3, 2, "uint", 1),
        ("timestamp", 5, 6, "ts", TS),
        ("event_code", 11, 1, "alpha", "Q"),
    ], "Start of Market hours. Stock Locate is always 0 for system events."),

    "R": ("stock_directory_aapl", [
        ("msg_type", 0, 1, "type", "R"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("stock", 11, 8, "alpha", "AAPL"),
        ("market_category", 19, 1, "alpha", "Q"),
        ("financial_status_indicator", 20, 1, "alpha", "N"),
        ("round_lot_size", 21, 4, "uint", 100),
        ("round_lots_only", 25, 1, "alpha", "N"),
        ("issue_classification", 26, 1, "alpha", "C"),
        ("issue_subtype", 27, 2, "alpha", "Z"),
        ("authenticity", 29, 1, "alpha", "P"),
        ("short_sale_threshold_indicator", 30, 1, "alpha", "N"),
        ("ipo_flag", 31, 1, "alpha", "N"),
        ("luld_reference_price_tier", 32, 1, "alpha", "1"),
        ("etp_flag", 33, 1, "alpha", "N"),
        ("etp_leverage_factor", 34, 4, "uint", 0),
        ("inverse_indicator", 38, 1, "alpha", "N"),
    ], "Longest run of single-byte alpha fields in the protocol (offsets 25-33). An off-by-one "
       "there does NOT change the message length, so a length check will not catch it - this "
       "fixture will. Note issue_subtype is 2 bytes: 'Z' padded to 'Z '."),

    "H": ("stock_trading_action_halt", [
        ("msg_type", 0, 1, "type", "H"),
        ("stock_locate", 1, 2, "uint", 42),
        ("tracking_number", 3, 2, "uint", 7),
        ("timestamp", 5, 6, "ts", TS),
        ("stock", 11, 8, "alpha", "TSLA"),
        ("trading_state", 19, 1, "alpha", "H"),
        ("reserved", 20, 1, "alpha", " "),
        ("reason", 21, 4, "alpha", "T1"),
    ], "Upper-case H. The reserved byte at offset 20 is a single space; omitting it shifts "
       "'reason' by one and still yields a 4-byte-looking string, so it fails silently. "
       "reason 'T1' is space-padded to 'T1  '."),

    "Y": ("reg_sho_restriction_in_effect", [
        ("msg_type", 0, 1, "type", "Y"),
        ("stock_locate", 1, 2, "uint", 42),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("stock", 11, 8, "alpha", "GME"),
        ("reg_sho_action", 19, 1, "alpha", "1"),
    ], "Same length and layout as N (RPII) - only the type character distinguishes them."),

    "L": ("market_participant_position_nsdq", [
        ("msg_type", 0, 1, "type", "L"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("mpid", 11, 4, "alpha", "NSDQ"),
        ("stock", 15, 8, "alpha", "AAPL"),
        ("primary_market_maker", 23, 1, "alpha", "Y"),
        ("market_maker_mode", 24, 1, "alpha", "N"),
        ("market_participant_state", 25, 1, "alpha", "A"),
    ], "MPID comes BEFORE stock here - the opposite of message F, where attribution comes last. "
       "Easy to transpose."),

    "V": ("mwcb_decline_levels", [
        ("msg_type", 0, 1, "type", "V"),
        ("stock_locate", 1, 2, "uint", 0),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("level_1", 11, 8, "price8", 253_456_789_000_000),
        ("level_2", 19, 8, "price8", 231_000_000_000_000),
        ("level_3", 27, 8, "price8", 190_500_000_000_000),
    ], "The ONLY Price(8) fields in the protocol: 8 implied decimals, divisor 100,000,000. "
       "Applying the Price(4) divisor of 10,000 gives answers 10,000x too large. "
       "level_1 raw 253456789000000 -> 2534567.89"),

    "W": ("mwcb_status_level_2_breached", [
        ("msg_type", 0, 1, "type", "W"),
        ("stock_locate", 1, 2, "uint", 0),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("breached_level", 11, 1, "alpha", "2"),
    ], "Byte-identical layout to S (System Event), both 12 bytes. Type char is the only "
       "discriminator - a decoder keyed on length alone will confuse them."),

    "K": ("ipo_quoting_period_update", [
        ("msg_type", 0, 1, "type", "K"),
        ("stock_locate", 1, 2, "uint", 0),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("stock", 11, 8, "alpha", "NEWCO"),
        ("ipo_quotation_release_time", 19, 4, "uint", 34_200),
        ("ipo_quotation_release_qualifier", 23, 1, "alpha", "A"),
        ("ipo_price", 24, 4, "price4", 170_000),
    ], "ipo_quotation_release_time is in SECONDS since midnight (34200 = 09:30:00), while the "
       "header timestamp is in NANOSECONDS. Do not apply the same scale. ipo_price raw 170000 "
       "-> $17.0000"),

    "J": ("luld_auction_collar", [
        ("msg_type", 0, 1, "type", "J"),
        ("stock_locate", 1, 2, "uint", 42),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("stock", 11, 8, "alpha", "TSLA"),
        ("auction_collar_reference_price", 19, 4, "price4", 2_000_000),
        ("upper_auction_collar_price", 23, 4, "price4", 2_100_000),
        ("lower_auction_collar_price", 27, 4, "price4", 1_900_000),
        ("auction_collar_extension", 31, 4, "uint", 2),
    ], "UPPER comes before LOWER here (message O has them the other way round). The final field "
       "is a plain integer COUNT of extensions, NOT a price - do not divide it by 10,000."),

    "h": ("operational_halt_bx", [
        ("msg_type", 0, 1, "type", "h"),
        ("stock_locate", 1, 2, "uint", 42),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("stock", 11, 8, "alpha", "TSLA"),
        ("market_code", 19, 1, "alpha", "B"),
        ("operational_halt_action", 20, 1, "alpha", "H"),
    ], "LOWER-CASE h. A dispatch table that upper-cases the type byte will route this to H "
       "(Stock Trading Action, 25 bytes) and then mis-frame everything after it."),

    # ---------------------------------------------------------------- order book
    "A": ("add_order_no_mpid_buy_aapl", [
        ("msg_type", 0, 1, "type", "A"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("order_reference_number", 11, 8, "uint", 4_294_967_297),
        ("buy_sell_indicator", 19, 1, "alpha", "B"),
        ("shares", 20, 4, "uint", 100),
        ("stock", 24, 8, "alpha", "AAPL"),
        ("price", 32, 4, "price4", 1_502_500),
    ], "The workhorse message. order_reference_number is 2**32+1, which is wrong in every "
       "32-bit-truncating decoder. price raw 1502500 -> $150.2500"),

    "F": ("add_order_mpid_sell_msft", [
        ("msg_type", 0, 1, "type", "F"),
        ("stock_locate", 1, 2, "uint", 2),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("order_reference_number", 11, 8, "uint", 4_294_967_298),
        ("buy_sell_indicator", 19, 1, "alpha", "S"),
        ("shares", 20, 4, "uint", 250),
        ("stock", 24, 8, "alpha", "MSFT"),
        ("price", 32, 4, "price4", 3_801_000),
        ("attribution", 36, 4, "alpha", "NSDQ"),
    ], "A is a strict 36-byte prefix of F. Same length (40) as Q (Cross Trade) but a completely "
       "different layout - length alone never identifies a message. price -> $380.1000"),

    "E": ("order_executed", [
        ("msg_type", 0, 1, "type", "E"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("order_reference_number", 11, 8, "uint", 4_294_967_297),
        ("executed_shares", 19, 4, "uint", 50),
        ("match_number", 23, 8, "uint", 8_589_934_592),
    ], "No price field - the execution is at the resting order's display price, which the "
       "decoder must look up from its own book state. executed_shares is a DELTA."),

    "C": ("order_executed_with_price_nonprintable", [
        ("msg_type", 0, 1, "type", "C"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("order_reference_number", 11, 8, "uint", 4_294_967_297),
        ("executed_shares", 19, 4, "uint", 25),
        ("match_number", 23, 8, "uint", 8_589_934_593),
        ("printable", 31, 1, "alpha", "N"),
        ("execution_price", 32, 4, "price4", 1_499_900),
    ], "printable='N': the book update still applies, but the trade must be EXCLUDED from "
       "time-and-sales and volume or it is double counted in a later bulk print. "
       "execution_price -> $149.9900. Same length (36) as A, different layout."),

    "X": ("order_cancel_partial", [
        ("msg_type", 0, 1, "type", "X"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("order_reference_number", 11, 8, "uint", 4_294_967_297),
        ("cancelled_shares", 19, 4, "uint", 30),
    ], "cancelled_shares is the number REMOVED, not the number remaining. Subtract it. "
       "Cumulative across repeated X messages for the same order reference."),

    "D": ("order_delete", [
        ("msg_type", 0, 1, "type", "D"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("order_reference_number", 11, 8, "uint", 4_294_967_297),
    ], "Shortest message in the protocol (19 bytes). Byte-identical layout to B (Broken Trade), "
       "but the 8-byte field is an ORDER REFERENCE number here and a MATCH number there."),

    "U": ("order_replace", [
        ("msg_type", 0, 1, "type", "U"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("original_order_reference_number", 11, 8, "uint", 4_294_967_297),
        ("new_order_reference_number", 19, 8, "uint", 4_294_967_400),
        ("shares", 27, 4, "uint", 75),
        ("price", 31, 4, "price4", 1_505_000),
    ], "shares here is the new ABSOLUTE total, unlike the deltas in X and E. Side, symbol and "
       "attribution are NOT in this message - the decoder must carry them over from the original "
       "Add Order, which is what forces it to keep an order-id -> (side, symbol, mpid) map. "
       "price -> $150.5000"),

    # ---------------------------------------------------------------- trades
    "P": ("trade_non_cross", [
        ("msg_type", 0, 1, "type", "P"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("order_reference_number", 11, 8, "uint", 0),
        ("buy_sell_indicator", 19, 1, "alpha", "B"),
        ("shares", 20, 4, "uint", 500),
        ("stock", 24, 8, "alpha", "AAPL"),
        ("price", 32, 4, "price4", 1_501_000),
        ("match_number", 36, 8, "uint", 8_589_934_594),
    ], "Longest order/trade message (44 bytes). Both degeneracies are set as the spec documents "
       "them: order_reference_number is 0 (null-filled since 2010-12-06) and buy_sell_indicator "
       "is 'B' (hardcoded since 2014-07-14) regardless of the true resting side. NEVER infer "
       "direction from this field. P does not affect the book but does count for volume. "
       "Byte-identical to A for its first 36 bytes."),

    "Q": ("cross_trade_opening", [
        ("msg_type", 0, 1, "type", "Q"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("shares", 11, 8, "uint", 4_294_967_396),
        ("stock", 19, 8, "alpha", "AAPL"),
        ("cross_price", 27, 4, "price4", 1_500_000),
        ("match_number", 31, 8, "uint", 8_589_934_595),
        ("cross_type", 39, 1, "alpha", "O"),
    ], "HIGHEST-VALUE FIXTURE IN THE SET. 'shares' is EIGHT bytes here, not four as in A/F/P. "
       "The value 2**32+100 exceeds 32 bits precisely so that a decoder using >I instead of >Q "
       "cannot pass by luck - it will also shift every following field. cross_price -> $150.0000"),

    "B": ("broken_trade", [
        ("msg_type", 0, 1, "type", "B"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("match_number", 11, 8, "uint", 8_589_934_594),
    ], "Same 19-byte layout as D but the 8-byte field is a MATCH number referring back to a "
       "previous E, C or P. A pure book builder may ignore B; anything computing volume or "
       "time-and-sales must process it."),

    # ---------------------------------------------------------------- auction / misc
    "I": ("noii_opening_cross_buy_imbalance", [
        ("msg_type", 0, 1, "type", "I"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("paired_shares", 11, 8, "uint", 4_294_967_396),
        ("imbalance_shares", 19, 8, "uint", 25_000),
        ("imbalance_direction", 27, 1, "alpha", "B"),
        ("stock", 28, 8, "alpha", "AAPL"),
        ("far_price", 36, 4, "price4", 1_499_000),
        ("near_price", 40, 4, "price4", 1_500_500),
        ("current_reference_price", 44, 4, "price4", 1_500_000),
        ("cross_type", 48, 1, "alpha", "O"),
        ("price_variation_indicator", 49, 1, "alpha", "L"),
    ], "Longest message except O (50 bytes). imbalance_direction sits BETWEEN the two 8-byte "
       "share counts and the stock symbol, so stock is at offset 28, not 27. paired_shares "
       "exceeds 32 bits deliberately."),

    "N": ("rpii_both_sides", [
        ("msg_type", 0, 1, "type", "N"),
        ("stock_locate", 1, 2, "uint", 1),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("stock", 11, 8, "alpha", "AAPL"),
        ("interest_flag", 19, 1, "alpha", "A"),
    ], "Same length and layout as Y (Reg SHO); only the type character differs."),

    "O": ("direct_listing_capital_raise", [
        ("msg_type", 0, 1, "type", "O"),
        ("stock_locate", 1, 2, "uint", 99),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", TS),
        ("stock", 11, 8, "alpha", "DLCRCO"),
        ("open_eligibility_status", 19, 1, "alpha", "Y"),
        ("minimum_allowable_price", 20, 4, "price4", 800_000),
        ("maximum_allowable_price", 24, 4, "price4", 1_800_000),
        ("near_execution_price", 28, 4, "price4", 1_000_000),
        ("near_execution_time", 32, 8, "uint", TS),
        ("lower_price_range_collar", 40, 4, "price4", 900_000),
        ("upper_price_range_collar", 44, 4, "price4", 1_100_000),
    ], "Added in the 2023-04-28 spec revision, so it appears in NO pre-2023 capture - this "
       "fixture is the only coverage it gets. near_execution_time is EIGHT bytes, unlike the "
       "6-byte header timestamp. LOWER collar precedes UPPER here, the reverse of message J."),

    # ---------------------------------------------------------------- boundary values
    "A_max_price": ("add_order_boundary_max_price", [
        ("msg_type", 0, 1, "type", "A"),
        ("stock_locate", 1, 2, "uint", 65_535),
        ("tracking_number", 3, 2, "uint", 65_535),
        ("timestamp", 5, 6, "ts", 281_474_976_710_655),
        ("order_reference_number", 11, 8, "uint", 18_446_744_073_709_551_615),
        ("buy_sell_indicator", 19, 1, "alpha", "S"),
        ("shares", 20, 4, "uint", 4_294_967_295),
        ("stock", 24, 8, "alpha", "ZZZZZZZZ"),
        ("price", 32, 4, "price4", 2_000_000_000),
    ], "ALL-MAXIMUM boundary case, type A. stock_locate and tracking_number at 0xFFFF; timestamp "
       "at the full 48-bit maximum 2**48-1 (= 78.2 hours, beyond any real trading day, but the "
       "widest legal encoding); order_reference_number at 2**64-1; shares at 2**32-1; stock "
       "filling all 8 bytes with no padding; price at exactly the spec maximum 0x77359400 = "
       "$200,000.0000. Any decoder using signed integers anywhere will return negatives here."),

    "A_zeros": ("add_order_boundary_all_zero", [
        ("msg_type", 0, 1, "type", "A"),
        ("stock_locate", 1, 2, "uint", 0),
        ("tracking_number", 3, 2, "uint", 0),
        ("timestamp", 5, 6, "ts", 0),
        ("order_reference_number", 11, 8, "uint", 0),
        ("buy_sell_indicator", 19, 1, "alpha", "B"),
        ("shares", 20, 4, "uint", 0),
        ("stock", 24, 8, "alpha", ""),
        ("price", 32, 4, "price4", 0),
    ], "ALL-ZERO boundary case, type A. Timestamp 0 = exactly midnight. The stock field is eight "
       "spaces (0x20 x 8), NOT eight NULs - a decoder that strips NULs instead of spaces returns "
       "'        ' here while one that strips spaces returns ''. Price 0 is the spec's "
       "not-applicable sentinel, not a $0.0000 quote."),
}


def encode_field(name, length, kind, value):
    """Assemble one field's bytes from its declared value. No struct, no format strings."""
    if kind in ("type", "alpha"):
        raw = str(value).encode("ascii")
        if len(raw) > length:
            raise ValueError(f"{name}: {value!r} is {len(raw)} bytes, field is {length}")
        return raw.ljust(length, b"\x20")          # SPACE padded, per spec section 1.4
    if kind in ("uint", "ts", "price4", "price8"):
        v = int(value)
        if not 0 <= v < (1 << (8 * length)):
            raise ValueError(f"{name}: {v} does not fit unsigned in {length} bytes")
        if kind == "price4" and v > PRICE4_MAX_RAW:
            raise ValueError(f"{name}: Price(4) {v} exceeds spec max {PRICE4_MAX_RAW}")
        return v.to_bytes(length, "big")           # BIG endian, per spec section 1.1
    raise ValueError(f"unknown kind {kind!r}")


def decoded_value(kind, value, length):
    """The human-facing expectation, derived from the declared literal - never from bytes."""
    if kind in ("type", "alpha"):
        return str(value)                          # already the stripped form
    if kind == "price4":
        q, r = divmod(int(value), PRICE4_SCALE)    # integer arithmetic only, no float
        return f"{q}.{r:04d}"
    if kind == "price8":
        q, r = divmod(int(value), PRICE8_SCALE)
        return f"{q}.{r:08d}"
    return int(value)


def build_one(key, slug, fields, note):
    msg_type = fields[0][4]
    total = sum(f[2] for f in fields)

    # contiguity / coverage: fields must tile [0, total) exactly
    cursor = 0
    for name, off, ln, _, _ in fields:
        if off != cursor:
            raise ValueError(f"{key}.{name}: offset {off} but previous field ends at {cursor}")
        cursor += ln
    if cursor != total:
        raise ValueError(f"{key}: fields cover {cursor} bytes, sum says {total}")

    # cross-check against the length recorded in the spec doc
    if SPEC_LENGTHS[msg_type] != total:
        raise ValueError(f"{key}: assembled {total} bytes, spec says {SPEC_LENGTHS[msg_type]}")

    buf = bytearray(total)
    seen = bytearray(total)                        # overlap detector
    for name, off, ln, kind, value in fields:
        buf[off:off + ln] = encode_field(name, ln, kind, value)
        for i in range(off, off + ln):
            if seen[i]:
                raise ValueError(f"{key}.{name}: byte {i} written twice")
            seen[i] = 1
    if not all(seen):
        raise ValueError(f"{key}: bytes {[i for i, s in enumerate(seen) if not s]} uncovered")

    body = bytes(buf)
    framed = len(body).to_bytes(2, "big") + body   # 2-byte BE length prefix, spec section 2.1

    (HERE / f"{key}_{slug}.itch").write_bytes(framed)

    expected = {
        "fixture": f"{key}_{slug}",
        "message_type": msg_type,
        "description": note,
        "framing": {
            "file_contains": "one framed message: 2-byte big-endian length prefix, then the body",
            "length_prefix": total,
            "length_prefix_hex": total.to_bytes(2, "big").hex(),
            "message_length_bytes": total,
            "total_file_bytes": len(framed),
            "note": "the length prefix counts the body INCLUDING its 1-byte type char and "
                    "EXCLUDING the 2 prefix bytes",
        },
        "message_hex": body.hex(),
        "framed_hex": framed.hex(),
        "endianness": "big",
        "expected_fields": [
            {
                "name": name,
                "offset": off,
                "length": ln,
                "type": {"type": "alpha", "alpha": "alpha", "uint": "integer",
                         "ts": "integer (nanoseconds since midnight, 6 bytes)",
                         "price4": "price(4)", "price8": "price(8)"}[kind],
                "raw_bytes_hex": body[off:off + ln].hex(),
                "raw_value": (str(value) if kind in ("type", "alpha") else int(value)),
                "decoded_value": decoded_value(kind, value, ln),
            }
            for name, off, ln, kind, value in fields
        ],
        "expected_result": "decode succeeds; every field equals decoded_value above",
    }
    (HERE / f"{key}_{slug}.json").write_text(json.dumps(expected, indent=2) + "\n")
    return msg_type, total, len(framed)


MALFORMED_CASES = [
    (
        "truncated_message",
        (36).to_bytes(2, "big") + bytes.fromhex("41000100001f1a3b9ac9fb410001000000000064"),
        {
            "expected_result": "error",
            "failure_mode": "truncated message body",
            "detail": "The length prefix declares 36 bytes (a valid Add Order length) but only 20 "
                      "bytes follow, then EOF. The reader must raise, not silently decode a short "
                      "message and not treat this as a clean end of file.",
            "length_prefix": 36,
            "bytes_actually_following": 20,
            "suggested_exception": "EOFError / TruncatedMessageError naming both 36 and 20",
        },
    ),
    (
        "unknown_message_type",
        (20).to_bytes(2, "big") + b"Z" + bytes(19),
        {
            "expected_result": "recoverable",
            "failure_mode": "unknown message type character",
            "detail": "Type 'Z' is not defined in ITCH 5.0. Because the 2-byte prefix carries the "
                      "length independently of the type table, this IS recoverable: consume the "
                      "20 declared bytes, count the message as unknown, and continue framing. A "
                      "decoder that aborts here cannot tolerate a future Nasdaq message type. A "
                      "decoder that skips a number of bytes taken from its type table rather than "
                      "from the prefix will desynchronise.",
            "length_prefix": 20,
            "unknown_type_char": "Z",
            "correct_behaviour": "skip exactly 20 bytes, increment an unknown-type counter, "
                                 "continue; do NOT raise",
        },
    ),
    (
        "bad_length_prefix_zero",
        (0).to_bytes(2, "big") + b"A" + bytes(35),
        {
            "expected_result": "error",
            "failure_mode": "zero length prefix",
            "detail": "A length prefix of 0 is never valid - no ITCH message is empty, the "
                      "shortest is 19 bytes (D/B). A reader that accepts it will loop forever "
                      "consuming zero bytes per iteration. Must raise. Note that valid-looking "
                      "Add Order bytes follow, so a decoder that ignores the prefix and trusts "
                      "the type byte will appear to succeed - that is the bug being caught.",
            "length_prefix": 0,
            "suggested_exception": "ValueError / BadFramingError; must not hang",
        },
    ),
    (
        "length_prefix_mismatch",
        (40).to_bytes(2, "big") + b"A" + bytes(39),
        {
            "expected_result": "error",
            "failure_mode": "length prefix disagrees with the message type",
            "detail": "The prefix declares 40 bytes and 40 bytes are present, so the read itself "
                      "succeeds - but type 'A' (Add Order, no MPID) is defined as 36 bytes; 40 is "
                      "the length of 'F'. This is framing desync, and unlike an unknown type it "
                      "is NOT recoverable by skipping: the stream position can no longer be "
                      "trusted. Must raise and report BOTH the prefix (40) and the expected "
                      "length for 'A' (36).",
            "length_prefix": 40,
            "type_char": "A",
            "expected_length_for_type": 36,
            "suggested_exception": "ValueError / FramingError quoting 40 and 36",
        },
    ),
    (
        "truncated_length_prefix",
        b"\x00",
        {
            "expected_result": "error",
            "failure_mode": "truncated length prefix",
            "detail": "A single byte where a 2-byte length prefix is expected. Distinguish this "
                      "from a CLEAN end of file, which is ZERO bytes available - zero must return "
                      "normally, one byte must raise. Getting this backwards either hides "
                      "corruption or reports a spurious error on every well-formed file.",
            "bytes_available": 1,
            "bytes_required": 2,
            "contrast": "0 bytes available => clean EOF, return normally, no exception",
            "suggested_exception": "EOFError / TruncatedStreamError",
        },
    ),
    (
        "timestamp_six_byte_trap",
        (19).to_bytes(2, "big") + b"D" + (1).to_bytes(2, "big") + (0).to_bytes(2, "big")
        + (281_474_976_710_655).to_bytes(6, "big") + (72_057_594_037_927_936).to_bytes(8, "big"),
        {
            "expected_result": "success",
            "failure_mode": "not malformed - a correctness trap for the 6-byte timestamp",
            "detail": "A well-formed 19-byte Order Delete. The timestamp is 2**48-1 (all six bytes "
                      "0xFF) and the order reference number is 2**56 (0x0100000000000000). A "
                      "decoder that wrongly reads EIGHT bytes at offset 5 gets timestamp "
                      "0xFFFFFFFFFFFF0100 and then runs off the end of the message. One that "
                      "reads only FOUR gets 4294967295. Only a correct 6-byte read yields both "
                      "declared values.",
            "expected_fields": {
                "msg_type": "D",
                "stock_locate": 1,
                "tracking_number": 0,
                "timestamp": 281474976710655,
                "order_reference_number": 72057594037927936,
            },
        },
    ),
]


def main():
    HERE.mkdir(parents=True, exist_ok=True)
    MALFORMED.mkdir(parents=True, exist_ok=True)

    index = {"valid": [], "malformed": []}
    for key, (slug, fields, note) in FIXTURES.items():
        t, total, framed_len = build_one(key, slug, fields, note)
        index["valid"].append({
            "key": key, "message_type": t, "message_length": total,
            "file_bytes": framed_len, "binary": f"{key}_{slug}.itch",
            "expected": f"{key}_{slug}.json", "fields": len(fields),
        })
        print(f"  {key:<12} type={t!r:<4} msg={total:>3}B  file={framed_len:>3}B  "
              f"fields={len(fields)}")

    for name, blob, meta in MALFORMED_CASES:
        (MALFORMED / f"{name}.itch").write_bytes(blob)
        meta = {"fixture": name, "file_bytes": len(blob), "hex": blob.hex(), **meta}
        (MALFORMED / f"{name}.json").write_text(json.dumps(meta, indent=2) + "\n")
        index["malformed"].append({
            "key": name, "file_bytes": len(blob),
            "binary": f"malformed/{name}.itch", "expected": f"malformed/{name}.json",
            "expected_result": meta["expected_result"],
        })
        print(f"  malformed/{name:<26} {len(blob):>3}B  -> {meta['expected_result']}")

    covered = {v["message_type"] for v in index["valid"]}
    index["coverage"] = {
        "message_types_covered": sorted(covered),
        "message_types_in_spec": sorted(SPEC_LENGTHS),
        "missing": sorted(set(SPEC_LENGTHS) - covered),
        "valid_fixtures": len(index["valid"]),
        "malformed_fixtures": len(index["malformed"]),
    }
    (HERE / "index.json").write_text(json.dumps(index, indent=2) + "\n")

    print(f"\n{len(index['valid'])} valid + {len(index['malformed'])} malformed fixtures")
    print(f"message types covered: {len(covered)}/{len(SPEC_LENGTHS)}")
    missing = index["coverage"]["missing"]
    print("MISSING TYPES:", missing if missing else "none - all 23 covered")


if __name__ == "__main__":
    main()
