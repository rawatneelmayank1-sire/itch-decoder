# Nasdaq TotalView-ITCH 5.0 — Implementable Decoder Reference

**Status:** every field offset, length and type in the message tables below was transcribed
directly from the official Nasdaq specification PDF (see Source), field by field, in this session.
Nothing in the message tables is from memory or from a third-party parser. Anything not confirmed
by that PDF is explicitly labelled `UNVERIFIED` or `NOT IN THIS SPEC`.

## Source

| | |
|---|---|
| Document | *Nasdaq TotalView-ITCH 5.0* |
| URL | `https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/NQTVITCHSpecification.pdf` |
| Retrieved | 2026-09-09 |
| Size | 1,200,722 bytes, 36 pages |
| Latest entry in the document's own revision log | **April 28, 2023** — introduction of the Direct Listing with Capital Raise message and the new NOII Imbalance Direction value "Paused" |
| Earlier entries noted | July 14, 2022 (ETP Leverage Factor wording); June 13, 2022 (subheading numbering) |
| Support contact named in the document | `clientsuccess@nasdaq.com` (Nasdaq Global Information Services) |

The specification is published by Nasdaq for use with its licensed data feed products. This file is
a derived engineering reference — offsets, lengths and widths, i.e. facts about a binary layout —
not a reproduction of the specification's prose.

---

## Part 1 — Wire fundamentals

These four rules cause more decoder bugs than all the message layouts combined.

### 1.1 Endianness — big-endian, always

The specification's Data Types section states that all integer fields are big endian (network byte
order) binary encoded numbers, and that unless otherwise noted they are unsigned.

**Why this matters on x86.** x86-64 and Apple Silicon are **little-endian**. Reading ITCH bytes
into a native integer without byte-swapping produces silently wrong numbers, not a crash:

- a 4-byte share count of `100` is `00 00 00 64` on the wire; read little-endian it becomes
  `1,677,721,600`;
- a `Price (4)` of `$25.0000` is `00 03 D0 90` = 250,000; read little-endian it becomes
  `0x90D00300` = 2,429,715,712.

Implications:

- In Python, **always** prefix the `struct` format with `>`. Never use `@` / `=` / no prefix. `@`
  (the default when no prefix is given) additionally applies **native alignment padding**, which
  silently corrupts every offset in every message in this protocol.
- `int.from_bytes(b, "big")` — the `"big"` is not optional.
- With NumPy, use explicit big-endian dtypes (`">u2"`, `">u4"`, `">u8"`) or `.byteswap()`. A plain
  `np.uint32` is native-endian and will be wrong.
- In C/C++: `ntohs` / `ntohl` / `be64toh`, or `__builtin_bswap*`.
- Never `#pragma pack`-and-cast a C struct over the buffer. Even with correct packing the byte
  order is still wrong on x86, and the 6-byte timestamp has no native type anyway.

### 1.2 Timestamps — 6 bytes, not 8. This is the classic bug.

The specification states: **timestamps are represented as nanoseconds since midnight.** In every
message type, the Timestamp field is at **offset 5** with **length 6**.

**Six bytes. Not eight.** This is the single most common ITCH decoding bug, because:

- 48 bits is not a native integer width. There is no `struct` code for it, no `uint48_t`, no NumPy
  dtype. Every language forces a manual widening, and the manual step is easy to get wrong.
- Reading **8** bytes at offset 5 consumes the first two bytes of the *next* field, shifting every
  subsequent field — and it usually still yields a plausible-looking (merely enormous) timestamp,
  so it fails silently rather than loudly.
- Reading **4** bytes drops the high 16 bits, so the value wraps every 2^32 ns ≈ **4.295 seconds**.
  The first few seconds of the day look correct and everything afterwards is wrong.

Three correct decodings:

```python
# A (preferred): slice 6 bytes, widen explicitly.
ts_ns = int.from_bytes(payload[5:11], "big")            # 0 .. 2**48-1

# B: let struct read it as 2 + 4 and recombine.
hi, lo = struct.unpack_from(">HI", payload, 5)          # 16 high bits, 32 low bits
ts_ns = (hi << 32) | lo

# C: left-pad to 8 bytes, then a single >Q.
ts_ns = struct.unpack(">Q", b"\x00\x00" + payload[5:11])[0]
```

Range and sanity notes:

- A 48-bit unsigned value tops out at 281,474,976,710,655 ns ≈ 78.2 hours. A full day is
  86,400 s = 8.64e13 ns, which needs 47 bits — so a day fits in 48 bits but **not** in 32.
- Nanoseconds since **local midnight, US/Eastern**, for the trading day the file covers. There is
  no date anywhere in the ITCH stream; the date comes from the filename or session metadata.
- Test invariants: timestamps are non-decreasing within a stream; `09:30:00 ET` =
  `34_200_000_000_000`; `16:00:00 ET` = `57_600_000_000_000`.
- Keep it as a 64-bit **integer**. Converting to float seconds destroys nanosecond resolution
  immediately (representing 1 ns relative to 5.76e4 s needs ~63 bits of mantissa; a double has 53).
  Format at the edge, never in the hot path.

### 1.3 Prices — fixed-point integers with implied decimals

The specification states that prices are integer fields supplied with an associated precision; when
converted to decimal they are fixed point, where the precision defines the number of decimal
places; a field flagged `Price (4)` has an implied 4 decimal places; and **the maximum value of
`Price (4)` in TotalView ITCH is 200,000.0000 decimal, `0x77359400` hex**.

| Wire type | Width | Implied decimals | Divisor for dollars | Example |
|---|---|---|---|---|
| `Price (4)` | 4 bytes, unsigned BE | 4 | `10_000` | `0x0003D090` = 250000 → `$25.0000` |
| `Price (8)` | 8 bytes, unsigned BE | 8 | `100_000_000` | only in message `V` (MWCB Decline Level) |

Note the asymmetry — it is a real trap. `Price (4)` has **4** implied decimals; `Price (8)` has
**8**. They are different *scales*, not merely different widths. The only `Price (8)` fields in the
protocol are the three MWCB decline levels in message `V`.

Ceiling check for validation: `Price (4)` max = 200,000.0000 = 2,000,000,000 raw =
`0x77359400`. A raw `Price (4)` above `0x77359400` is out of spec and is good evidence of a framing
error.

**Why prices must stay integers internally.**

1. **Binary floats cannot represent decimal cents.** `0.1` and `0.01` are not representable in
   IEEE-754. `$25.10` as a double is `25.09999999999999786...`. Two orders both at `$25.10` can
   then compare unequal, which splits what should be one price level in the book into two.
2. **An order book is keyed by price.** If the key is a float, equality and hashing are unreliable
   and sorted-container ordering can be perturbed by representation error. If the key is an `int`
   number of ticks, all of it is exact and total.
3. **Round-tripping is lossless.** `int` → decimal string is exact. `float` → string → `float` is
   not reliably so, and accumulated notional (`price * shares` summed over millions of messages)
   drifts. In integers, notional in ten-thousandths of a dollar stays exact in `int64`.
4. **It is faster.** Integer compare and add beat float, with no NaN/`-0.0` normalisation.

Discipline to follow: decode `Price (4)` into an `int` holding the **raw** integer (units of
1/10,000 dollar). Expose a separate accessor that scales for display, using
`decimal.Decimal(raw).scaleb(-4)` or integer divmod — **not** float division. Every comparison,
book key, spread and VWAP numerator stays integral.

Sentinel: `0` means "not applicable" in several places (e.g. IPO Price in message `K` when the
quotation period is cancelled or postponed — the spec says both IPO Quotation Time and IPO Price
are set to 0). Treat `0` as a sentinel, not as a $0.0000 quote.

### 1.4 Alpha fields — fixed-width, space-padded, ASCII

The specification states that all alpha fields are ASCII, left justified, and padded on the right
with spaces.

- Padding is `0x20` (space), **not** `0x00`. Do not `rstrip("\x00")`.
- Fixed width. No length byte, no terminator.
- `Stock` is 8 bytes: `AAPL` is `41 41 50 4C 20 20 20 20`. Decode ASCII then `.rstrip()` to get
  `"AAPL"`. Class-suffixed symbols use more of the field; all 8 bytes may be significant.
- `MPID` / `Attribution` is 4 bytes, same rules (`NSDQ`, ...).
- `Reason` in message `H` is 4 alpha bytes and is very often **all spaces**. All-spaces is a
  legitimate value meaning "no reason given", not a decode failure.
- Single-byte alpha fields (Buy/Sell Indicator, Event Code, Trading State, ...) are one ASCII
  character. In Python `struct`, `c` yields `bytes` of length 1 and `Ns` yields `bytes`. Decide
  once whether records hold `bytes` or `str` and be consistent — `b"B" != "B"` is a classic bug.
- Compare against the stripped value, never the raw padded field.

### 1.5 Integers

Unsigned unless stated otherwise (which, for these message types, it never is). Widths in use:
2 bytes (`>H`), 4 bytes (`>I`), 6 bytes (timestamp only, see §1.2), 8 bytes (`>Q`).

- `Order Reference Number` and `Match Number` are 8 bytes and day-unique. They are **not**
  guaranteed dense or monotonic — use a hash map, never an array indexed by order id.
- `Stock Locate` is 2 bytes and **is** designed as an array index: the spec describes it as a low
  integer assigned dynamically from 1 each day, communicated by the Stock Directory message, so it
  can serve as an array index for fast instrument lookup. It appears at the same position in every
  message to support cheap filtering. `0` means "not stock dependent". It is **not** stable across
  days — never cache a locate code overnight.
- `Tracking Number` (2 bytes, offset 3) is a Nasdaq-internal value. Decode it, do not interpret it.

---

## Part 2 — The outer framing

**Scoping note, stated plainly.** The TotalView-ITCH 5.0 PDF describes only the *message payloads*.
It explicitly delegates transport to a higher-level protocol — it says the messages are typically
delivered using a higher level protocol that handles sequencing and delivery guarantees, and names
three options: **SoupBinTCP**, **compressed SoupBinTCP**, and **MoldUDP64**. The PDF contains no
length-prefix table. Do not go looking for one there.

### 2.1 File format — a 2-byte big-endian length prefix before every message

The downloadable historical daily files (`*.NASDAQ_ITCH50.gz`) are stored in the flat "BinaryFILE"
layout: MoldUDP64 message blocks concatenated with their per-message framing preserved.

```
+--------+--------+-------------------------------------------+
| len_hi | len_lo |  message body, exactly `len` bytes         |
+--------+--------+-------------------------------------------+
  uint16, big-endian          body[0] is the message-type char
```

- The prefix is **unsigned 16-bit big-endian** (`>H`).
- The length counts the body **including** its 1-byte message-type character and **excluding** the
  2 prefix bytes. For an Add Order (`A`), the prefix reads `0x0024` = 36 and 36 bytes follow, the
  first of which is `0x41` = `'A'`.
- Messages are packed back to back. No padding, no alignment, no separators.
- The stream ends at EOF. The logical end of day is the `S` System Event with Event Code `C`.

Reader loop:

```python
def frames(f):
    while True:
        hdr = f.read(2)
        if not hdr:                       # clean EOF
            return
        if len(hdr) < 2:
            raise EOFError("truncated length prefix")
        n = int.from_bytes(hdr, "big")
        if n == 0:
            raise ValueError("bad length prefix: 0")
        body = f.read(n)
        if len(body) < n:
            raise EOFError(f"truncated message: wanted {n}, got {len(body)}")
        yield body
```

Validation the decoder should perform:

| Condition | Meaning | Handling |
|---|---|---|
| 0 bytes available for the prefix | clean end of file | stop, success |
| 1 byte available for the prefix | truncated stream | raise: truncated length prefix |
| `n == 0` | never valid — no ITCH message is empty | raise: bad length prefix |
| `body[0]` not in the type table | unknown / newer message type | **recoverable**: `n` bytes were already consumed, count it and continue |
| `n != EXPECTED_LEN[body[0]]` | framing desync | **not** recoverable by skipping; raise, reporting both the prefix and the expected length |
| `len(body) < n` | truncated final message | raise: truncated message |

The key asymmetry: because the prefix carries the length independently of the type table, an
**unknown type is recoverable** (skip `n` and carry on). A **length that disagrees** with the type
table is not — it means framing has been lost and every subsequent read is garbage.

### 2.2 MoldUDP64 and SoupBinTCP (live feeds) — `UNVERIFIED`

For orientation only, if the decoder is ever pointed at a live capture instead of a daily file:

- **MoldUDP64** datagram: 10-byte Session (alpha), 8-byte Sequence Number, 2-byte Message Count,
  then that many message blocks, each a 2-byte big-endian length followed by the payload. The
  per-message framing is therefore identical to §2.1 — which is why the file format looks as it
  does.
- **SoupBinTCP** packet: 2-byte big-endian packet length, 1-byte packet type (`S` = sequenced data,
  carrying an ITCH message; `H` = heartbeat; etc.), then the payload. Here the length **includes**
  the type byte.

**`UNVERIFIED`.** §2.2 was **not** read from the TotalView-ITCH PDF (it is not in it) and no
separate primary document was fetched for it in this session. The file framing in §2.1 is what the
decoder actually needs and is stated with confidence. Check the MoldUDP64 and SoupBinTCP specs on
nasdaqtrader.com before implementing a live reader.

---

## Part 3 — The common header

Every message type in TotalView-ITCH 5.0 begins with the same 11-byte header. This is verified
across all 21 message tables in the PDF; there are no exceptions.

| Field | Offset | Length | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | one ASCII character, the type discriminator |
| Stock Locate | 1 | 2 | Integer | instrument index; `0` when not stock-dependent |
| Tracking Number | 3 | 2 | Integer | Nasdaq internal |
| Timestamp | 5 | **6** | Integer | nanoseconds since midnight — see §1.2 |

Header `struct` format: `>cHH6s` (`struct.calcsize` = 11).

Message bodies begin at **offset 11** in every case.

---

## Part 4 — Message reference

Conventions used in every table below:

- **Offset** is measured from the start of the message body, i.e. the message-type character is at
  offset 0. It does **not** include the 2-byte file length prefix.
- **Total length** is the message length *excluding* the 2-byte length prefix — it is exactly the
  value that appears in that prefix.
- Two `struct` format strings are given for each type:
  - **full** — decodes the whole message from offset 0, including the type character;
  - **body** — decodes from offset 1, for decoders that dispatch on the type byte first.
  In both, the 6-byte timestamp appears as `6s`; widen it with
  `int.from_bytes(raw, "big")` (§1.2).
- `struct.calcsize(full)` equals the stated total length for every type. This was checked
  programmatically.

### Quick length table

| Type | Message | Length (bytes) | Section |
|---|---|---:|---|
| `S` | System Event | 12 | 4.1 |
| `R` | Stock Directory | 39 | 4.2 |
| `H` | Stock Trading Action | 25 | 4.3 |
| `Y` | Reg SHO Restriction | 20 | 4.4 |
| `L` | Market Participant Position | 26 | 4.5 |
| `V` | MWCB Decline Level | 35 | 4.6 |
| `W` | MWCB Status | 12 | 4.7 |
| `K` | IPO Quoting Period Update | 28 | 4.8 |
| `J` | LULD Auction Collar | 35 | 4.9 |
| `h` | Operational Halt (lower-case) | 21 | 4.10 |
| `A` | Add Order — No MPID Attribution | 36 | 4.11 |
| `F` | Add Order — With MPID Attribution | 40 | 4.12 |
| `E` | Order Executed | 31 | 4.13 |
| `C` | Order Executed With Price | 36 | 4.14 |
| `X` | Order Cancel | 23 | 4.15 |
| `D` | Order Delete | 19 | 4.16 |
| `U` | Order Replace | 35 | 4.17 |
| `P` | Trade (Non-Cross) | 44 | 4.18 |
| `Q` | Cross Trade | 40 | 4.19 |
| `B` | Broken Trade | 19 | 4.20 |
| `I` | NOII (Net Order Imbalance Indicator) | 50 | 4.21 |
| `N` | Retail Price Improvement Indicator | 20 | 4.22 |
| `O` | Direct Listing with Capital Raise | 48 | 4.23 |

**`h` is lower-case** and is a different message from `H`. Dispatch tables keyed by a
case-insensitive character will collide. This is the second most common ITCH bug after the 6-byte
timestamp.

---

### 4.1 `S` — System Event

Signals a market or data-feed handler event. **Total length: 12 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"S"` |
| Stock Locate | 1 | 2 | Integer | always 0 |
| Tracking Number | 3 | 2 | Integer | Nasdaq internal |
| Timestamp | 5 | 6 | Integer | nanoseconds since midnight |
| Event Code | 11 | 1 | Alpha | see codes below |

- full: `>cHH6sc` (12)
- body: `>HH6sc` (11)

Event Codes:

| Code | Meaning |
|---|---|
| `O` | Start of Messages — first message of the trading day |
| `S` | Start of System hours — Nasdaq is ready to accept orders |
| `Q` | Start of Market hours — market-hours orders available for execution |
| `M` | End of Market hours |
| `E` | End of System hours — no new orders accepted; Broken Trade and Order Delete messages may still follow |
| `C` | End of Messages — always the last message of the trading day |

---

### 4.2 `R` — Stock Directory

Disseminated at the start of each trading day for all active symbols; this is what assigns the
Stock Locate codes. **Total length: 39 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"R"` |
| Stock Locate | 1 | 2 | Integer | locate code assigned to this symbol for the day |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Stock | 11 | 8 | Alpha | symbol, right-padded with spaces |
| Market Category | 19 | 1 | Alpha | listing market / tier |
| Financial Status Indicator | 20 | 1 | Alpha | |
| Round Lot Size | 21 | 4 | Integer | shares in a round lot |
| Round Lots Only | 25 | 1 | Alpha | `Y` / `N` |
| Issue Classification | 26 | 1 | Alpha | see spec appendix |
| Issue Sub-Type | 27 | 2 | Alpha | see spec appendix |
| Authenticity | 29 | 1 | Alpha | `P` live/production, `T` test |
| Short Sale Threshold Indicator | 30 | 1 | Alpha | `Y` / `N` / space |
| IPO Flag | 31 | 1 | Alpha | `Y` / `N` / space |
| LULD Reference Price Tier | 32 | 1 | Alpha | `1` / `2` / space |
| ETP Flag | 33 | 1 | Alpha | `Y` / `N` / space |
| ETP Leverage Factor | 34 | 4 | Integer | rounded down to nearest integer |
| Inverse Indicator | 38 | 1 | Alpha | `Y` / `N` |

- full: `>cHH6s8sccIcc2scccccIc` (39)
- body: `>HH6s8sccIcc2scccccIc` (38)

Market Category: `Q` Nasdaq Global Select, `G` Nasdaq Global, `S` Nasdaq Capital Market,
`N` NYSE, `A` NYSE American, `P` NYSE Arca, `Z` BATS Z, `V` Investors' Exchange (IEX),
`<space>` not available.

Financial Status Indicator (Nasdaq-listed): `D` deficient, `E` delinquent, `Q` bankrupt,
`S` suspended, `G` deficient+bankrupt, `H` deficient+delinquent, `J` delinquent+bankrupt,
`K` deficient+delinquent+bankrupt, `C` creations/redemptions suspended for ETP, `N` normal
(default). `<space>` for non-Nasdaq-listed.

Note the two adjacent single-byte fields at 25/26 and the run of five at 29–33: an off-by-one here
does not change the message length, so it will not be caught by a length check. Use the fixture
for `R` to pin it.

---

### 4.3 `H` — Stock Trading Action

Current trading status of a security. **Total length: 25 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"H"` (upper-case) |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Stock | 11 | 8 | Alpha | right-padded with spaces |
| Trading State | 19 | 1 | Alpha | `H` halted, `P` paused (Nasdaq-listed only), `Q` quotation-only period, `T` trading on Nasdaq |
| Reserved | 20 | 1 | Alpha | reserved — decode it, do not interpret it |
| Reason | 21 | 4 | Alpha | trading action reason; frequently all spaces |

- full: `>cHH6s8scc4s` (25)
- body: `>HH6s8scc4s` (24)

The `Reserved` byte at offset 20 is real and occupies space. Omitting it shifts `Reason` by one and
still yields a 4-byte-looking string, so it fails quietly.

---

### 4.4 `Y` — Reg SHO Short Sale Price Test Restricted Indicator

**Total length: 20 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"Y"` |
| Locate Code | 1 | 2 | Integer | (the spec names this field "Locate Code" here, not "Stock Locate"; same field) |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Stock | 11 | 8 | Alpha | |
| Reg SHO Action | 19 | 1 | Alpha | `0` no price test, `1` restriction in effect due to intraday price drop, `2` restriction remains in effect |

- full: `>cHH6s8sc` (20)
- body: `>HH6s8sc` (19)

---

### 4.5 `L` — Market Participant Position

**Total length: 26 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"L"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| MPID | 11 | 4 | Alpha | market participant identifier |
| Stock | 15 | 8 | Alpha | |
| Primary Market Maker | 23 | 1 | Alpha | `Y` / `N` |
| Market Maker Mode | 24 | 1 | Alpha | `N` normal, `P` passive, `S` syndicate, `R` pre-syndicate, `L` penalty |
| Market Participant State | 25 | 1 | Alpha | `A` active, `E` excused/withdrawn, `W` withdrawn, `S` suspended, `D` deleted |

- full: `>cHH6s4s8sccc` (26)
- body: `>HH6s4s8sccc` (25)

Note: MPID comes **before** Stock in this message — the opposite of the `F` Add Order layout, where
Attribution comes last. Easy to transpose.

---

### 4.6 `V` — MWCB Decline Level

Daily market-wide circuit breaker breach points. **Total length: 35 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"V"` |
| Stock Locate | 1 | 2 | Integer | always 0 |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Level 1 | 11 | 8 | **Price (8)** | MWCB Level 1 value |
| Level 2 | 19 | 8 | **Price (8)** | MWCB Level 2 value |
| Level 3 | 27 | 8 | **Price (8)** | MWCB Level 3 value |

- full: `>cHH6sQQQ` (35)
- body: `>HH6sQQQ` (34)

**The only `Price (8)` fields in the protocol.** Divisor is `100_000_000`, not `10_000`. Applying
the `Price (4)` divisor here yields values 10,000× too large.

---

### 4.7 `W` — MWCB Status

**Total length: 12 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"W"` |
| Stock Locate | 1 | 2 | Integer | always 0 |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Breached Level | 11 | 1 | Alpha | `1` / `2` / `3` |

- full: `>cHH6sc` (12)
- body: `>HH6sc` (11)

`W` and `S` have identical lengths and identical layouts. They are distinguished only by the type
character.

---

### 4.8 `K` — IPO Quoting Period Update

**Total length: 28 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"K"` |
| Stock Locate | 1 | 2 | Integer | spec says always set to 0 |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Stock | 11 | 8 | Alpha | |
| IPO Quotation Release Time | 19 | 4 | Integer | **seconds** since midnight, to the nearest second |
| IPO Quotation Release Qualifier | 23 | 1 | Alpha | `A` anticipated release time, `C` release cancelled/postponed |
| IPO Price | 24 | 4 | Price (4) | |

- full: `>cHH6s8sIcI` (28)
- body: `>HH6s8sIcI` (27)

Two traps here. (1) `IPO Quotation Release Time` is in **seconds**, while the header Timestamp is in
**nanoseconds** — do not apply the same scale. (2) When the quotation period is cancelled or
postponed, the spec says the quotation time **and** the IPO price are both set to `0`.

---

### 4.9 `J` — LULD Auction Collar

**Total length: 35 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"J"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Stock | 11 | 8 | Alpha | |
| Auction Collar Reference Price | 19 | 4 | Price (4) | |
| Upper Auction Collar Price | 23 | 4 | Price (4) | |
| Lower Auction Collar Price | 27 | 4 | Price (4) | |
| Auction Collar Extension | 31 | 4 | **Integer** | number of extensions to the reopening auction |

- full: `>cHH6s8sIIII` (35)
- body: `>HH6s8sIIII` (34)

The last field is a plain count, **not** a price. Do not divide it by 10,000. Also note the order:
**Upper** comes before **Lower**.

---

### 4.10 `h` — Operational Halt (lower-case `h`)

**Total length: 21 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"h"` — **lower-case** |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Stock | 11 | 8 | Alpha | |
| Market Code | 19 | 1 | Alpha | `Q` Nasdaq, `B` BX, `X` PSX |
| Operational Halt Action | 20 | 1 | Alpha | `H` operationally halted on that market, `T` halt lifted, trading resumed |

- full: `>cHH6s8scc` (21)
- body: `>HH6s8scc` (20)

An Operational Halt is specific to the market centre for which it is declared and, unlike `H`
(Stock Trading Action), does not stop the instrument trading elsewhere.

---

### 4.11 `A` — Add Order, No MPID Attribution

A new displayable order was accepted and added to the book. **Total length: 36 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"A"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Order Reference Number | 11 | 8 | Integer | day-unique order id |
| Buy/Sell Indicator | 19 | 1 | Alpha | `B` buy, `S` sell |
| Shares | 20 | 4 | Integer | total shares added to the book |
| Stock | 24 | 8 | Alpha | |
| Price | 32 | 4 | Price (4) | display price |

- full: `>cHH6sQcI8sI` (36)
- body: `>HH6sQcI8sI` (35)

---

### 4.12 `F` — Add Order With MPID Attribution

**Total length: 40 bytes.** Identical to `A` for the first 36 bytes, plus a 4-byte attribution.

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"F"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Order Reference Number | 11 | 8 | Integer | |
| Buy/Sell Indicator | 19 | 1 | Alpha | `B` / `S` |
| Shares | 20 | 4 | Integer | |
| Stock | 24 | 8 | Alpha | |
| Price | 32 | 4 | Price (4) | |
| Attribution | 36 | 4 | Alpha | Nasdaq MPID associated with the order |

- full: `>cHH6sQcI8sI4s` (40)
- body: `>HH6sQcI8sI4s` (39)

Because `A` is a strict prefix of `F`, one handler can decode both and only read Attribution when
the type is `F`. Book-building treats them identically.

---

### 4.13 `E` — Order Executed

An order on the book was executed, in whole or in part, at its display price. **Total length: 31
bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"E"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Order Reference Number | 11 | 8 | Integer | |
| Executed Shares | 19 | 4 | Integer | |
| Match Number | 23 | 8 | Integer | day-unique; referenced by Broken Trade (`B`) |

- full: `>cHH6sQIQ` (31)
- body: `>HH6sQIQ` (30)

No price field — the execution happened at the order's display price, which the decoder must look
up from its own book state. Multiple `E` messages for one order reference are **cumulative**;
subtract from the resting quantity and remove the order when it reaches zero.

---

### 4.14 `C` — Order Executed With Price

An order executed at a price **different** from its display price. **Total length: 36 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"C"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Order Reference Number | 11 | 8 | Integer | |
| Executed Shares | 19 | 4 | Integer | |
| Match Number | 23 | 8 | Integer | |
| Printable | 31 | 1 | Alpha | `Y` printable, `N` non-printable |
| Execution Price | 32 | 4 | Price (4) | price at which the execution occurred |

- full: `>cHH6sQIQcI` (36)
- body: `>HH6sQIQcI` (35)

`Printable = N` means the shares will be included in a later bulk print (e.g. a cross). Firms
building time-and-sales or volume statistics should **ignore non-printable executions** to avoid
double-counting. The book update still applies regardless of `Printable`.

---

### 4.15 `X` — Order Cancel

Partial cancellation: shares removed from the display size of a resting order. **Total length: 23
bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"X"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Order Reference Number | 11 | 8 | Integer | order being cancelled |
| Cancelled Shares | 19 | 4 | Integer | shares **removed**, not the shares remaining |

- full: `>cHH6sQI` (23)
- body: `>HH6sQI` (22)

`Cancelled Shares` is a **delta**. Subtract it. Effects are cumulative across multiple `X` messages
for the same order reference; when displayed shares reach zero the order is dead and must be
removed from the book.

---

### 4.16 `D` — Order Delete

The order is fully cancelled; all remaining shares are gone. **Total length: 19 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"D"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Order Reference Number | 11 | 8 | Integer | order being deleted |

- full: `>cHH6sQ` (19)
- body: `>HH6sQ` (18)

Shortest order-book message. Remove the order outright. `D` and `B` have the same length and the
same layout — distinguish by type character only.

---

### 4.17 `U` — Order Replace

Cancel-replace. The original order is removed and a new one takes its place under a **new** order
reference number. **Total length: 35 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"U"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Original Order Reference Number | 11 | 8 | Integer | order being replaced — remove it |
| New Order Reference Number | 19 | 8 | Integer | used for all subsequent updates |
| Shares | 27 | 4 | Integer | new **total** displayed quantity (absolute, not a delta) |
| Price | 31 | 4 | Price (4) | new display price |

- full: `>cHH6sQQII` (35)
- body: `>HH6sQQII` (34)

Side, stock symbol and attribution **cannot change** in a replace, so they are not in the message.
The decoder must carry them over from the original Add Order. This is the message that forces the
decoder to keep an order-id → (side, symbol, mpid) map rather than being stateless.

Also note: `Shares` here is an **absolute** new quantity, unlike the deltas in `X` and `E`.

---

### 4.18 `P` — Trade Message (Non-Cross)

Execution of a **non-displayable** order, for which no Add Order was ever published. **Total
length: 44 bytes.** The longest of the order/trade messages.

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"P"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Order Reference Number | 11 | 8 | Integer | **populated as zero** since 2010-12-06 (null-filled bytes) |
| Buy/Sell Indicator | 19 | 1 | Alpha | **always `"B"`** since 2014-07-14, regardless of the resting side |
| Shares | 20 | 4 | Integer | shares matched |
| Stock | 24 | 8 | Alpha | |
| Price | 32 | 4 | Price (4) | match price |
| Match Number | 36 | 8 | Integer | referenced by Broken Trade (`B`) |

- full: `>cHH6sQcI8sIQ` (44)
- body: `>HH6sQcI8sIQ` (43)

Two documented degeneracies, both stated in the spec: the Order Reference Number has been zero
since 6 December 2010, and the Buy/Sell Indicator has been hardcoded `B` since 14 July 2014. **Do
not** infer trade direction from this field, and do not treat the zero order reference as a lookup
key. `P` messages do **not** affect the book — they are pure prints — but they must be included in
time-and-sales and volume statistics.

Layout note: `P` is byte-identical to `A` for its first 36 bytes, then appends an 8-byte Match
Number where `F` appends a 4-byte Attribution. Length is the only structural discriminator.

---

### 4.19 `Q` — Cross Trade

Bulk print for a cross event. **Total length: 40 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"Q"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Shares | 11 | **8** | Integer | shares matched in the cross |
| Stock | 19 | 8 | Alpha | |
| Cross Price | 27 | 4 | Price (4) | |
| Match Number | 31 | 8 | Integer | |
| Cross Type | 39 | 1 | Alpha | `O` opening cross, `C` closing cross, `H` IPO / halted-paused cross |

- full: `>cHH6sQ8sIQc` (40)
- body: `>HH6sQ8sIQc` (39)

**`Shares` is 8 bytes here**, not 4 as in `A`/`F`/`P`. Cross volumes can be enormous, so the field
was widened. Using `>I` here shifts every following field. This is the highest-value single check
in the whole message set — pin it with the `Q` fixture.

`Q` and `F` are both 40 bytes but have completely different layouts. Length alone does not identify
a message; always dispatch on the type character and use the length only as a cross-check.

Shares may legitimately be `0` if there was insufficient interest to cross. Non-printable cross
transactions should be excluded from time-and-sales to avoid double counting.

---

### 4.20 `B` — Broken Trade / Order Execution

A previously reported execution has been broken. Final and irreversible. **Total length: 19 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"B"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Match Number | 11 | 8 | Integer | Match Number of a previously sent `E`, `C` or `P` message |

- full: `>cHH6sQ` (19)
- body: `>HH6sQ` (18)

Same length and layout as `D`, different semantics — the 8-byte field is a **Match** Number, not an
**Order Reference** Number. A pure book builder can ignore `B`; anything computing volume or
time-and-sales must process it.

---

### 4.21 `I` — Net Order Imbalance Indicator (NOII)

**Total length: 50 bytes.** The longest message in the protocol after `O`.

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"I"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Paired Shares | 11 | 8 | Integer | shares eligible to match at the Current Reference Price |
| Imbalance Shares | 19 | 8 | Integer | shares not paired at the Current Reference Price |
| Imbalance Direction | 27 | 1 | Alpha | `B` buy, `S` sell, `N` none, `O` insufficient orders to calculate, `P` paused |
| Stock | 28 | 8 | Alpha | |
| Far Price | 36 | 4 | Price (4) | hypothetical clearing price, cross orders only |
| Near Price | 40 | 4 | Price (4) | hypothetical clearing price, cross + continuous orders |
| Current Reference Price | 44 | 4 | Price (4) | price at which NOII shares are calculated |
| Cross Type | 48 | 1 | Alpha | `O` opening, `C` closing, `H` IPO/halted-paused, `A` Extended Trading Close |
| Price Variation Indicator | 49 | 1 | Alpha | see table below |

- full: `>cHH6sQQc8sIIIcc` (50)
- body: `>HH6sQQc8sIIIcc` (49)

Note that `Imbalance Direction` sits **between** the two 8-byte share counts and the `Stock` field —
Stock is at offset 28, not 27. Both share counts are 8 bytes.

Price Variation Indicator: `L` < 1%, `1` 1–1.99%, `2` 2–2.99%, `3` 3–3.99%, `4` 4–4.99%,
`5` 5–5.99%, `6` 6–6.99%, `7` 7–7.99%, `8` 8–8.99%, `9` 9–9.99%, `A` 10–19.99%, `B` 20–29.99%,
`C` 30% or greater, `<space>` cannot be calculated.

`P` (Paused) as an Imbalance Direction and `A` (Extended Trading Close) as a Cross Type are
additions from the April 2023 revision. A decoder written against an older PDF will not know them.

---

### 4.22 `N` — Retail Price Improvement Indicator (RPII)

**Total length: 20 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"N"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Stock | 11 | 8 | Alpha | |
| Interest Flag | 19 | 1 | Alpha | `B` RPI on buy side, `S` RPI on sell side, `A` both, `N` none |

- full: `>cHH6s8sc` (20)
- body: `>HH6s8sc` (19)

Same length and layout as `Y`; distinguished only by the type character.

---

### 4.23 `O` — Direct Listing with Capital Raise Price Discovery

Disseminated only for DLCR securities, once per second once the DLCR volatility test has passed.
Added in the **April 28, 2023** revision. **Total length: 48 bytes.**

| Field | Offset | Len | Type | Notes |
|---|---:|---:|---|---|
| Message Type | 0 | 1 | Alpha | `"O"` |
| Stock Locate | 1 | 2 | Integer | |
| Tracking Number | 3 | 2 | Integer | |
| Timestamp | 5 | 6 | Integer | |
| Stock | 11 | 8 | Alpha | |
| Open Eligibility Status | 19 | 1 | Alpha | `Y` eligible to be released for trading, `N` not eligible |
| Minimum Allowable Price | 20 | 4 | Price (4) | 20% below Registration Statement lower price |
| Maximum Allowable Price | 24 | 4 | Price (4) | 80% above Registration Statement highest price |
| Near Execution Price | 28 | 4 | Price (4) | current reference price once the volatility test passed |
| Near Execution Time | 32 | **8** | Integer | time at which the Near Execution Price was set |
| Lower Price Range Collar | 40 | 4 | Price (4) | 10% below Near Execution Price |
| Upper Price Range Collar | 44 | 4 | Price (4) | 10% above Near Execution Price |

- full: `>cHH6s8scIIIQII` (48)
- body: `>HH6s8scIIIQII` (47)

`Near Execution Time` is an **8-byte** integer, unlike the 6-byte header timestamp. The spec does
not state its unit in the field table; by analogy with the header it is nanoseconds since midnight,
but that inference is `UNVERIFIED`. Decode it as a raw `uint64` and do not scale it without
confirming.

Note the collar order here is **Lower then Upper** — the opposite of message `J`, where Upper comes
first.

---

## Part 5 — Machine-readable summary

Drop-in tables for a decoder. Every `calcsize` was checked programmatically against the
"Total length" stated in Part 4.

```python
# Message length INCLUDING the 1-byte type char, EXCLUDING the 2-byte file length prefix.
# This is exactly the value carried in the file's length prefix.
MESSAGE_LENGTHS = {
    b"S": 12,   # System Event
    b"R": 39,   # Stock Directory
    b"H": 25,   # Stock Trading Action
    b"Y": 20,   # Reg SHO Restriction
    b"L": 26,   # Market Participant Position
    b"V": 35,   # MWCB Decline Level
    b"W": 12,   # MWCB Status
    b"K": 28,   # IPO Quoting Period Update
    b"J": 35,   # LULD Auction Collar
    b"h": 21,   # Operational Halt   (lower-case h)
    b"A": 36,   # Add Order, no MPID
    b"F": 40,   # Add Order, with MPID
    b"E": 31,   # Order Executed
    b"C": 36,   # Order Executed With Price
    b"X": 23,   # Order Cancel
    b"D": 19,   # Order Delete
    b"U": 35,   # Order Replace
    b"P": 44,   # Trade (non-cross)
    b"Q": 40,   # Cross Trade
    b"B": 19,   # Broken Trade
    b"I": 50,   # NOII
    b"N": 20,   # RPII
    b"O": 48,   # Direct Listing with Capital Raise
}

# Full-message formats: offset 0, including the type char. 6s = the 6-byte timestamp.
STRUCT_FULL = {
    b"S": ">cHH6sc",
    b"R": ">cHH6s8sccIcc2scccccIc",
    b"H": ">cHH6s8scc4s",
    b"Y": ">cHH6s8sc",
    b"L": ">cHH6s4s8sccc",
    b"V": ">cHH6sQQQ",
    b"W": ">cHH6sc",
    b"K": ">cHH6s8sIcI",
    b"J": ">cHH6s8sIIII",
    b"h": ">cHH6s8scc",
    b"A": ">cHH6sQcI8sI",
    b"F": ">cHH6sQcI8sI4s",
    b"E": ">cHH6sQIQ",
    b"C": ">cHH6sQIQcI",
    b"X": ">cHH6sQI",
    b"D": ">cHH6sQ",
    b"U": ">cHH6sQQII",
    b"P": ">cHH6sQcI8sIQ",
    b"Q": ">cHH6sQ8sIQc",
    b"B": ">cHH6sQ",
    b"I": ">cHH6sQQc8sIIIcc",
    b"N": ">cHH6s8sc",
    b"O": ">cHH6s8scIIIQII",
}

# Body formats: offset 1, after the type char has been consumed by the dispatcher.
STRUCT_BODY = {k: ">" + v[2:] for k, v in STRUCT_FULL.items()}

# Field names, positionally aligned with STRUCT_FULL (the leading "msg_type" included).
FIELD_NAMES = {
    b"S": ("msg_type", "stock_locate", "tracking_number", "timestamp", "event_code"),
    b"R": ("msg_type", "stock_locate", "tracking_number", "timestamp", "stock",
           "market_category", "financial_status_indicator", "round_lot_size",
           "round_lots_only", "issue_classification", "issue_subtype", "authenticity",
           "short_sale_threshold_indicator", "ipo_flag", "luld_reference_price_tier",
           "etp_flag", "etp_leverage_factor", "inverse_indicator"),
    b"H": ("msg_type", "stock_locate", "tracking_number", "timestamp", "stock",
           "trading_state", "reserved", "reason"),
    b"Y": ("msg_type", "stock_locate", "tracking_number", "timestamp", "stock",
           "reg_sho_action"),
    b"L": ("msg_type", "stock_locate", "tracking_number", "timestamp", "mpid", "stock",
           "primary_market_maker", "market_maker_mode", "market_participant_state"),
    b"V": ("msg_type", "stock_locate", "tracking_number", "timestamp",
           "level_1", "level_2", "level_3"),
    b"W": ("msg_type", "stock_locate", "tracking_number", "timestamp", "breached_level"),
    b"K": ("msg_type", "stock_locate", "tracking_number", "timestamp", "stock",
           "ipo_quotation_release_time", "ipo_quotation_release_qualifier", "ipo_price"),
    b"J": ("msg_type", "stock_locate", "tracking_number", "timestamp", "stock",
           "auction_collar_reference_price", "upper_auction_collar_price",
           "lower_auction_collar_price", "auction_collar_extension"),
    b"h": ("msg_type", "stock_locate", "tracking_number", "timestamp", "stock",
           "market_code", "operational_halt_action"),
    b"A": ("msg_type", "stock_locate", "tracking_number", "timestamp",
           "order_reference_number", "buy_sell_indicator", "shares", "stock", "price"),
    b"F": ("msg_type", "stock_locate", "tracking_number", "timestamp",
           "order_reference_number", "buy_sell_indicator", "shares", "stock", "price",
           "attribution"),
    b"E": ("msg_type", "stock_locate", "tracking_number", "timestamp",
           "order_reference_number", "executed_shares", "match_number"),
    b"C": ("msg_type", "stock_locate", "tracking_number", "timestamp",
           "order_reference_number", "executed_shares", "match_number", "printable",
           "execution_price"),
    b"X": ("msg_type", "stock_locate", "tracking_number", "timestamp",
           "order_reference_number", "cancelled_shares"),
    b"D": ("msg_type", "stock_locate", "tracking_number", "timestamp",
           "order_reference_number"),
    b"U": ("msg_type", "stock_locate", "tracking_number", "timestamp",
           "original_order_reference_number", "new_order_reference_number", "shares",
           "price"),
    b"P": ("msg_type", "stock_locate", "tracking_number", "timestamp",
           "order_reference_number", "buy_sell_indicator", "shares", "stock", "price",
           "match_number"),
    b"Q": ("msg_type", "stock_locate", "tracking_number", "timestamp", "shares", "stock",
           "cross_price", "match_number", "cross_type"),
    b"B": ("msg_type", "stock_locate", "tracking_number", "timestamp", "match_number"),
    b"I": ("msg_type", "stock_locate", "tracking_number", "timestamp", "paired_shares",
           "imbalance_shares", "imbalance_direction", "stock", "far_price", "near_price",
           "current_reference_price", "cross_type", "price_variation_indicator"),
    b"N": ("msg_type", "stock_locate", "tracking_number", "timestamp", "stock",
           "interest_flag"),
    b"O": ("msg_type", "stock_locate", "tracking_number", "timestamp", "stock",
           "open_eligibility_status", "minimum_allowable_price", "maximum_allowable_price",
           "near_execution_price", "near_execution_time", "lower_price_range_collar",
           "upper_price_range_collar"),
}

# Fields that are Price(4): divide by 10_000 for dollars (display only — keep raw ints).
PRICE4_FIELDS = {
    b"K": ("ipo_price",),
    b"J": ("auction_collar_reference_price", "upper_auction_collar_price",
           "lower_auction_collar_price"),
    b"A": ("price",),
    b"F": ("price",),
    b"C": ("execution_price",),
    b"U": ("price",),
    b"P": ("price",),
    b"Q": ("cross_price",),
    b"I": ("far_price", "near_price", "current_reference_price"),
    b"O": ("minimum_allowable_price", "maximum_allowable_price", "near_execution_price",
           "lower_price_range_collar", "upper_price_range_collar"),
}

# Fields that are Price(8): divide by 100_000_000. Message V only.
PRICE8_FIELDS = {b"V": ("level_1", "level_2", "level_3")}

PRICE4_SCALE = 10_000
PRICE8_SCALE = 100_000_000
PRICE4_MAX_RAW = 0x77359400          # 200,000.0000 — anything above is out of spec
TIMESTAMP_OFFSET, TIMESTAMP_LENGTH = 5, 6
HEADER_LENGTH = 11
```

---

## Part 6 — Implementation checklist

Ranked by how often each one actually bites.

1. **Timestamp is 6 bytes.** Never `>Q` at offset 5. See §1.2.
2. **`>` on every struct format.** No prefix means native order *and* native alignment padding.
3. **`h` (Operational Halt) is lower-case and is not `H` (Stock Trading Action).** Do not
   upper-case the type byte before dispatching.
4. **`Q` Cross Trade has an 8-byte `Shares` field**, unlike `A`/`F`/`P` which use 4.
5. **`V` uses `Price (8)`** with 8 implied decimals — divisor 100,000,000, not 10,000.
6. **`H` has a `Reserved` byte at offset 20.** Omitting it silently shifts `Reason`.
7. **Prices stay `int`.** Scale only when formatting for a human. See §1.3.
8. **`X` and `E` carry deltas; `U` carries an absolute quantity.** Mixing them corrupts the book.
9. **`U` omits side / symbol / attribution.** Carry them over from the original Add Order.
10. **`P` has a hardcoded `B` side and a zero order reference.** Never infer direction from it.
11. **Length is not a discriminator.** `S`/`W` (12), `D`/`B` (19), `Y`/`N` (20), `A`/`C` (36),
    `F`/`Q` (40), `V`/`J`/`U` (35) all collide. Dispatch on the type char; use the length as a
    cross-check only.
12. **Alpha padding is spaces, not NULs.** `rstrip()`, not `rstrip("\x00")`.
13. **Stock Locate is not stable across days.** Rebuild the mapping from that day's `R` spin.
14. **Unknown type ⇒ skip `n` bytes and continue. Length mismatch ⇒ hard error.** See §2.1.
15. **`C` with `Printable = N`** still updates the book but must be excluded from
    time-and-sales/volume.
16. **`K`'s IPO Quotation Release Time is in seconds**, not nanoseconds.
17. **Collar field order differs between `J` (Upper, then Lower) and `O` (Lower, then Upper).**

---

## Part 7 — Verification status

**Verified against the official PDF, field by field, in this session** (offset, length and type for
every field): `S`, `R`, `H`, `Y`, `L`, `V`, `W`, `K`, `J`, `h`, `A`, `F`, `E`, `C`, `X`, `D`, `U`,
`P`, `Q`, `B`, `I`, `N`, `O` — all 23 message types, plus the Data Types section (endianness,
alpha padding, price precision and the 200,000.0000 / `0x77359400` maximum), and the Architecture
section (transport options, Stock Locate semantics).

Every `struct` format string in Part 5 was checked with `struct.calcsize` against the message
length recorded from the PDF; all 23 agree.

### Empirical confirmation against real Nasdaq data

The length table in Part 5 was additionally checked against a real production capture:
**12,554,295 messages** from `01302019.NASDAQ_ITCH50.gz` (Nasdaq's own published sample for
30 January 2019), decoded using only §2.1 framing and the `MESSAGE_LENGTHS` table above.

| Check | Result |
|---|---|
| Frames whose 2-byte length prefix disagreed with the spec length table | **0** of 12,554,295 |
| Unknown message-type characters encountered | **0** |
| Trailing bytes left over after the final frame | **0** — the stream framed perfectly end to end |
| Timestamps out of non-decreasing order | **0** |
| Timestamp range covered | 03:03:59 → 09:30:30 ET (pre-market through the open) |
| Largest raw `Price (4)` seen in `A`/`F` messages | 1,999,999,900 = `$199,999.9900` — just under the spec's stated maximum of `$200,000.0000` (2,000,000,000), confirming §1.3 |

Message types **empirically observed** and correctly framed (16 of 23): `A` (4,745,681),
`D` (4,015,409), `X` (1,084,447), `I` (1,051,216), `F` (685,473), `U` (661,033), `L` (193,722),
`E` (62,564), `P` (18,870), `Y` (8,720), `H` (8,716), `R` (8,714), `Q` (8,713), `C` (1,013),
`S` (3), `V` (1).

Message types **not present in that capture** (7 of 23): `B`, `J`, `K`, `N`, `O`, `W`, `h`. Their
absence is expected, not evidence of error — `B` (broken trades), `W` (MWCB breach), `J` (LULD
collar), `K` (IPO quoting) and `h` (operational halt) are rare event messages that simply did not
occur, and `O` (DLCR) did not exist until the April 2023 revision, four years after this capture.
For these seven types the layout rests on the PDF transcription alone, which is why the
hand-built fixtures in `data/fixtures/` cover all of them.

Reproduce with `data/prefix_validation.json` (full-scan results) and
`data/sample_truncated.stats.json`.

**Marked `UNVERIFIED` — do not implement from this document alone:**

| Item | Where | Why |
|---|---|---|
| MoldUDP64 header layout (session/sequence/count) | §2.2 | not in the TotalView-ITCH PDF; no primary source fetched this session |
| SoupBinTCP packet header layout | §2.2 | same |
| Unit of `Near Execution Time` in message `O` | §4.23 | the PDF's field table gives type and width but not the unit; nanoseconds-since-midnight is an inference by analogy |
| Issue Classification and Issue Sub-Type value tables (`R`, offsets 26 and 27) | §4.2 | the PDF defers these to an appendix that was not transcribed here; the **offsets and widths are verified**, only the enumerated values are missing |

**Deliberately out of scope:** GLIMPSE retransmission, the full Issue Classification appendix, and
the FPGA feed's operational differences (the spec states the FPGA payload is byte-identical to the
software feed, so it does not affect decoding).

Anything not in the tables above and not listed as `UNVERIFIED` was not encountered in the source
document. If a field appears in a real capture that this document does not describe, treat the
document as wrong and re-check the source PDF rather than guessing.
