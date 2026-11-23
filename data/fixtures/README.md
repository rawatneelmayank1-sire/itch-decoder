# ITCH 5.0 validation fixtures

Byte-exact test vectors for a Nasdaq TotalView-ITCH 5.0 decoder. **31 fixtures: 25 valid
(covering all 23 message types) + 6 malformed/trap cases.**

Built by hand from `docs/itch-5.0-spec.md`, which was transcribed field by field from the official
Nasdaq specification PDF.

## Why these are trustworthy

They are **independent ground truth**: they were not produced by, derived from, or checked against
the decoder in `src/`. Specifically:

- `build_fixtures.py` imports nothing but stdlib `json` and `pathlib`. It never touches `src/`.
- Field **values are declared first**, as Python literals. The bytes are assembled *from* those
  values — each field written at its literal declared offset with `int.to_bytes(n, "big")` or
  ASCII + space padding.
- The assembler deliberately **does not use `struct`** or the spec's format strings, so an error in
  a format string cannot leak into a fixture and then "confirm itself" on decode.
- The JSON expectations come from the same declared literals, not from decoding the bytes.

Build-time self-checks (all abort the build on failure): fields tile `[0, total)` with no gaps and
no overlaps; the assembled length equals the length recorded in the spec; every alpha value fits
its field; every integer fits its width unsigned; every `Price(4)` is within `0x77359400`.

A separate cross-check then decodes all 25 fixtures using the spec's `struct` format strings — a
different code path from the one that built them — and confirms all **209 fields** round-trip to
their declared values, with framing, price scaling, raw-byte hex and space-padding all agreeing.

## Layout

```
fixtures/
  index.json                  manifest + coverage report
  build_fixtures.py           the generator (idempotent; re-run to regenerate)
  <KEY>_<slug>.itch           one FRAMED message: 2-byte BE length prefix + body
  <KEY>_<slug>.json           expected decode, field by field
  malformed/<name>.itch       deliberately broken input
  malformed/<name>.json       expected failure mode
```

**The `.itch` files are framed** — a 2-byte big-endian length prefix followed by the message body,
exactly as the message appears in a `.NASDAQ_ITCH50` file. So `file_bytes = message_length + 2`.
To test a bare message decoder instead, skip the first 2 bytes, or use `message_hex` from the JSON
(`framed_hex` has the prefix included).

Each JSON gives, per field: `name`, `offset`, `length`, `type`, `raw_bytes_hex`, `raw_value` and
`decoded_value`. Prices carry the raw integer **and** an exactly-computed decimal string
(integer `divmod`, never float) — a decoder should match the raw integer; the string is what a
formatter should produce.

## Valid fixtures — all 23 message types

| Key | Type | Msg bytes | What it pins down |
|---|---|---:|---|
| `S` | `S` | 12 | System Event, Start of Market hours |
| `R` | `R` | 39 | Stock Directory — the 9 consecutive single-byte alpha fields at offsets 25–33 |
| `H` | `H` | 25 | Stock Trading Action — the **reserved byte** at offset 20 |
| `Y` | `Y` | 20 | Reg SHO — same shape as `N` |
| `L` | `L` | 26 | Market Participant Position — **MPID before Stock** |
| `V` | `V` | 35 | **The only `Price(8)` fields in the protocol** |
| `W` | `W` | 12 | MWCB Status — byte-identical layout to `S` |
| `K` | `K` | 28 | IPO Quoting — release time in **seconds**, not nanoseconds |
| `J` | `J` | 35 | LULD collar — **Upper before Lower**; last field is a count, not a price |
| `h` | `h` | 21 | Operational Halt — **lower-case `h`** |
| `A` | `A` | 36 | Add Order — order ref `2**32+1` breaks 32-bit truncation |
| `F` | `F` | 40 | Add Order + MPID — same length as `Q`, different layout |
| `E` | `E` | 31 | Order Executed — no price field |
| `C` | `C` | 36 | Executed With Price, `printable='N'` |
| `X` | `X` | 23 | Order Cancel — `cancelled_shares` is a **delta** |
| `D` | `D` | 19 | Order Delete — shortest message |
| `U` | `U` | 35 | Order Replace — `shares` is **absolute**, not a delta |
| `P` | `P` | 44 | Trade non-cross — order ref `0`, side hardcoded `B` |
| `Q` | `Q` | 40 | **Cross Trade — `shares` is 8 bytes, not 4.** Highest-value fixture |
| `B` | `B` | 19 | Broken Trade — `D`'s layout, but a **match** number |
| `I` | `I` | 50 | NOII — stock at offset **28**, not 27 |
| `N` | `N` | 20 | RPII — same shape as `Y` |
| `O` | `O` | 48 | DLCR — 2023-only message, **no capture contains it** |
| `A_max_price` | `A` | 36 | All-maximum boundary: 48-bit ts max, `2**64-1` order ref, price exactly `$200,000.0000` |
| `A_zeros` | `A` | 36 | All-zero boundary: ts 0, and an all-**spaces** stock field |

### The three highest-value fixtures

1. **`Q_cross_trade_opening`** — `shares` is **8 bytes** here, unlike the 4 in `A`/`F`/`P`. The
   value is `2**32+100`, chosen so a decoder using `>I` cannot pass by luck; it will also shift
   every subsequent field.
2. **`malformed/timestamp_six_byte_trap`** — despite living in `malformed/`, this is a **valid**
   message. Timestamp is `2**48-1` (six `0xFF` bytes) and the order ref is `2**56`. Reading 8 bytes
   at offset 5 yields `0xFFFFFFFFFFFF0100` and overruns; reading 4 yields `4294967295`. Only a
   correct 6-byte read gives both declared values.
3. **`A_max_price`** — every field at its unsigned maximum. Any decoder using signed integers
   anywhere returns negatives here.

## Malformed fixtures

| File | Bytes | Expected | Failure mode |
|---|---:|---|---|
| `truncated_message` | 22 | **error** | prefix says 36, only 20 bytes follow, then EOF |
| `unknown_message_type` | 22 | **recoverable** | type `Z` — skip the 20 declared bytes and continue; do **not** raise |
| `bad_length_prefix_zero` | 38 | **error** | prefix `0`; must raise and must not loop forever |
| `length_prefix_mismatch` | 42 | **error** | prefix 40 but type `A` is 36 — framing desync, **not** recoverable by skipping |
| `truncated_length_prefix` | 1 | **error** | 1 byte where 2 are needed — must be distinguished from clean EOF at 0 bytes |
| `timestamp_six_byte_trap` | 21 | **success** | see above |

The two subtleties worth stating outright:

- **Unknown type is recoverable; a length mismatch is not.** The prefix carries the length
  independently of the type table, so an unrecognised type can be skipped safely — that is what
  lets a decoder survive a future Nasdaq message type. A prefix that *disagrees* with a known
  type's length means stream position is no longer trustworthy, so skipping compounds the damage.
- **0 bytes available is a clean EOF; 1 byte is an error.** Getting this backwards either hides
  corruption or reports a spurious error on every well-formed file.

## Suggested use

```python
import json, pathlib

FX = pathlib.Path("data/fixtures")

def test_all_valid_fixtures():
    for entry in json.loads((FX / "index.json").read_text())["valid"]:
        expected = json.loads((FX / entry["expected"]).read_text())
        framed = (FX / entry["binary"]).read_bytes()
        assert int.from_bytes(framed[:2], "big") == expected["framing"]["length_prefix"]
        msg = decode(framed[2:])                       # your decoder
        for f in expected["expected_fields"]:
            assert getattr(msg, f["name"]) == f["raw_value"], (entry["key"], f["name"])
```

Regenerate with `python data/fixtures/build_fixtures.py` (idempotent — identical bytes every run).

## Relationship to the real capture

`data/sample_truncated.itch` (real exchange data, so kept out of the repository; rebuild it with
`data/PROVENANCE.md` §7) is 500,000 real pre-market messages and covers 12 message types
heavily, but it contains **no** `B`, `J`, `K`, `N`, `O`, `W` or `h`, and — being pre-market — almost
no executions and no cross trades. Even the full 409 MB prefix covers only 16 of 23 types.

These fixtures are the only coverage the remaining 7 types get, and the only source of boundary
values and malformed input. Use fixtures for **correctness**, the capture for **volume and
throughput**.
