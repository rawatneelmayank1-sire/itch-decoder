# Data provenance — `data/`

Recorded 2026-09-09. Everything below is a statement of what was actually done. No file described
here is synthetic, and no download is claimed that did not happen.

---

## 1. Source

| | |
|---|---|
| Host | `https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/` |
| Publisher | The Nasdaq Stock Market, LLC |
| Directory listing retrieved | 2026-09-09 |
| Protocol notes | Plain HTTPS directory index, anonymous access, no authentication. `Accept-Ranges: bytes` confirmed by HTTP HEAD, so byte-range requests work. |

### Full `.gz` inventory observed in that directory, 2026-09-09

Sorted by size. HEAD requests were issued before any transfer.

| Bytes | Size | File | Listed date |
|---:|---:|---|---|
| 294,755,242 | 0.27 GiB | `tvagg.gz` | 2023-05-15 |
| 3,524,013,057 | 3.28 GiB | `12302019.NASDAQ_ITCH50.gz` | 2019-12-31 |
| 3,662,140,094 | 3.41 GiB | `07302019.NASDAQ_ITCH50.gz` | 2019-07-31 |
| 3,872,931,242 | 3.61 GiB | `10302019.NASDAQ_ITCH50.gz` | 2019-10-31 |
| 3,951,201,663 | 3.68 GiB | `S101819-v50.txt.gz` | 2019-10-19 |
| 4,075,649,457 | 3.80 GiB | `08302019.NASDAQ_ITCH50.gz` | 2019-08-31 |
| 4,735,308,661 | 4.41 GiB | `S112825-v50.txt.gz` | 2026-08-05 |
| **4,764,426,091** | **4.44 GiB** | **`01302019.NASDAQ_ITCH50.gz`** | **2019-01-31** |
| 4,889,328,604 | 4.55 GiB | `S081321-v50.txt.gz` | 2021-08-13 |
| 5,510,131,732 | 5.13 GiB | `03272019.NASDAQ_ITCH50.gz` | 2019-03-28 |
| 5,597,158,940 | 5.21 GiB | `01302020.NASDAQ_ITCH50.gz` | 2020-01-31 |
| 5,996,745,270 | 5.58 GiB | `S071321-v50.txt.gz` | 2021-07-13 |
| 7,929,915,419 | 7.39 GiB | `S120925-v50.txt.gz` | 2026-08-05 |
| 8,775,891,119 | 8.17 GiB | `S120825-v50.txt.gz` | 2026-08-05 |
| 10,471,034,001 | 9.75 GiB | `S121125-v50.txt.gz` | 2026-08-05 |
| 11,557,662,295 | 10.76 GiB | `S121025-v50.txt.gz` | 2026-08-05 |
| 12,551,644,054 | 11.69 GiB | `S121225-v50.txt.gz` | 2026-08-05 |
| 13,047,496,628 | 12.15 GiB | `itch50_05_15.gz` | 2026-06-09 |
| 16,150,095,810 | 15.04 GiB | `itch50_05_18.gz` | 2026-06-09 |
| 17,894,268,560 | 16.67 GiB | `S061226-v50.txt.gz` | 2026-06-23 |

### The size decision

**No complete file was downloaded, and none should be.**

- The smallest `NASDAQ_ITCH50` daily file is `12302019.NASDAQ_ITCH50.gz` at **3,524,013,057 bytes
  (3.28 GiB compressed)** — roughly **7× the ~500 MB download budget** for this project, and it
  expands to something on the order of 10 GiB uncompressed.
- `tvagg.gz` (294 MB) is the only object under 500 MB, but it is **not** a TotalView-ITCH 5.0 daily
  message stream — it is a different aggregated product and is not usable as a decoder fixture. It
  was **not** downloaded.
- Therefore no full file was downloaded.

### What was fetched instead: a byte-range prefix

The server advertises `Accept-Ranges: bytes`, and gzip streams decompress from the start, so an
HTTP **Range** request for the first N bytes yields a valid, decompressible prefix of the real
trading day. That is what exists on disk.

| | |
|---|---|
| URL | `https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/01302019.NASDAQ_ITCH50.gz` |
| Full remote size (HEAD `Content-Length`) | 4,764,426,091 bytes |
| Remote `Last-Modified` | Thu, 31 Jan 2019 05:18:15 GMT |
| Trading day represented | **2019-01-30** |
| Bytes actually transferred | **157,286,400** (150.0 MiB) — a Range request for the first 150 MiB, **3.3 %** of the file |
| Downloaded by | `tools_download.py`, in resumable 8 MiB Range chunks. The result was then validated end to end without re-fetching; see §4. |
| Date | 2026-09-09 |

---

## 2. Files on disk

| File | Bytes | What it is | In the repository? |
|---|---:|---|---|
| `01302019.NASDAQ_ITCH50.prefix.gz` | 157,286,400 | The raw 150 MiB HTTP-Range prefix of the real Nasdaq file, as transferred. Not a complete gzip member — it is a valid *prefix*, so `gunzip` will decompress it and then report an unexpected EOF. That is expected. | **no — gitignored** |
| `01302019.NASDAQ_ITCH50.prefix.itch` | 409,227,774 | The above, decompressed and cut back to the last **whole** ITCH message. Contains **12,554,295** complete framed messages. | **no — gitignored** |
| `sample_truncated.itch` | **14,409,954** (13.74 MiB) | The first **500,000** messages of the real stream, byte-for-byte, with framing intact. | **no — gitignored** (real exchange messages); rebuild it with §7 |
| `sample_truncated.stats.json` | small | Message counts, timestamp range and type histogram for `sample_truncated.itch`. | yes |
| `prefix_validation.json` | small | Full-scan validation results over all 12,554,295 messages. | yes |
| `download.log` | 1,213 | Transfer log written by `tools_download.py`. | no — local log |
| `fixtures/` | small | Hand-built byte-exact test vectors. See `fixtures/README.md`. | yes |

---

## 3. Precisely what was extracted into `sample_truncated.itch`

Method: the decompressed prefix was walked using **only** the outer framing rule from
`docs/itch-5.0-spec.md` §2.1 — read a 2-byte big-endian length, read that many bytes, repeat — and
the first 500,000 frames were copied **verbatim**, including their length prefixes. No field was
re-encoded, reordered, filtered or normalised. The file is a byte-exact prefix of the real Nasdaq
stream, and any decoder that reads the original file will read this identically.

| | |
|---|---|
| Messages | 500,000 |
| Bytes | 14,409,954 (13.74 MiB) |
| Mean framed message size | 28.8 bytes |
| First timestamp | 11,039,687,760,787 ns = **03:03:59.687760787 ET** |
| Last timestamp | 15,014,632,907,888 ns = **04:10:14.632907888 ET** |
| Framing errors | **0** |

Message type histogram:

| Type | Count | | Type | Count |
|---|---:|---|---|---:|
| `L` Market Participant Position | 193,665 | | `R` Stock Directory | 8,713 |
| `A` Add Order | 114,411 | | `H` Stock Trading Action | 8,711 |
| `D` Order Delete | 107,898 | | `Y` Reg SHO Restriction | 8,711 |
| `X` Order Cancel | 45,946 | | `E` Order Executed | 321 |
| `U` Order Replace | 11,549 | | `F` Add Order w/ MPID | 39 |
| | | | `P` Trade (non-cross) | 34 |
| | | | `S` System Event | 2 |

This window is **pre-market only** (03:04–04:10 ET), which shapes the mix: it is dominated by the
start-of-day administrative spins (8,713 `R` + 8,711 `H` + 8,711 `Y`, one per active symbol) and
by market-maker quote churn (`L`, `A`, `D`, `X`). It contains almost no executions and **no**
`Q` cross trades or `I` imbalance messages, because those happen at and around the 09:30 open.

**Implication for whoever tests against this file:** `sample_truncated.itch` exercises the
administrative and order-entry paths thoroughly but barely touches the execution path. Use
`data/fixtures/` for per-type correctness (all 23 types, including the ones no capture contains),
and the full 409 MB prefix — which does reach 09:30:30 and contains 1,051,216 `I` and 8,713 `Q`
messages — if you need the open.

---

## 4. Independent validation performed

The full 409 MB prefix (**12,554,295 messages**) was scanned end to end using only the spec's
framing rule and length table:

| Check | Result |
|---|---|
| Length prefix disagreeing with the spec length table | **0** |
| Unknown message-type characters | **0** |
| Trailing partial bytes after the final frame | **0** — framed perfectly end to end |
| Timestamps out of non-decreasing order | **0** |
| Timestamp span | 03:03:59 → 09:30:30 ET |
| Max raw `Price (4)` in `A`/`F` | 1,999,999,900 = `$199,999.9900`, just under the spec maximum of `$200,000.0000` |

16 of the 23 message types appear and frame correctly: `A`, `D`, `X`, `I`, `F`, `U`, `L`, `E`,
`P`, `Y`, `H`, `R`, `Q`, `C`, `S`, `V`. Absent: `B`, `J`, `K`, `N`, `O`, `W`, `h` — all rare event
messages, and `O` post-dates this 2019 capture entirely. Raw results: `prefix_validation.json`.

---

## 5. Licence and terms

Nasdaq publishes these historical sample files for anonymous download at `emi.nasdaq.com` for
development and testing of ITCH feed handlers. No click-through licence, authentication or
registration gate was encountered, and no terms-of-use document was presented at the download
point.

That is a description of what was observed, **not** a legal clearance. Before any redistribution,
commercial use, or publication of derived data:

- consult Nasdaq Global Information Services (`clientsuccess@nasdaq.com`), and
- note that real-time and licensed TotalView products are governed by separate Nasdaq data
  agreements that this historical sample does not waive.

This repository therefore keeps every file of raw exchange messages local, `sample_truncated.itch`
included, and commits only derived artefacts (statistics, charts, the downsampled book snapshots
behind the web viewer) and the hand-built fixtures, which contain no market data.

---

## 6. What the repository contains

The two large captures are **566 MB** combined, and GitHub rejects single files above 100 MB
outright. `sample_truncated.itch` is small enough to commit but is raw exchange data, so it stays
local too. The repository `.gitignore` ignores everything under `data/` except the files marked
"yes" below:

```gitignore
data/*
!data/PROVENANCE.md
!data/prefix_validation.json
!data/sample_truncated.stats.json
!data/fixtures/
*.itch
!data/fixtures/**/*.itch
*.gz
*.NASDAQ_ITCH50*
data/sample_truncated.itch
```

| Path | Bytes | In the repository? |
|---|---:|---|
| `data/01302019.NASDAQ_ITCH50.prefix.gz` | 157,286,400 | **no** |
| `data/01302019.NASDAQ_ITCH50.prefix.itch` | 409,227,774 | **no** |
| `data/sample_truncated.itch` | 14,409,954 | **no** — rebuild it with §7 |
| `data/download.log` | 1,213 | no |
| `data/sample_truncated.stats.json`, `data/prefix_validation.json` | 1,083 | yes |
| `data/fixtures/**` (65 files) | 121,077 | yes |

Neither the test suite nor CI reads any of the ignored files: the tests run against the synthetic
generator, and `data/fixtures/` plus the generator are the correctness ground truth. The sample is
a convenience for volume and throughput work.

## 7. Reproducing the data from scratch

The large files and the sample are fully reproducible; nothing here is a one-off artefact.
`python tools_download.py` performs steps 2 and 3.

1. `HEAD https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/01302019.NASDAQ_ITCH50.gz` → confirm
   `Content-Length: 4764426091` and `Accept-Ranges: bytes`.
2. `GET` the same URL with `Range: bytes=0-157286399` → `01302019.NASDAQ_ITCH50.prefix.gz`.
3. Decompress with a streaming gzip reader, tolerating the truncated tail, and cut back to the last
   whole ITCH message → `01302019.NASDAQ_ITCH50.prefix.itch`.
4. Walk the 2-byte big-endian length framing and copy the first 500,000 frames verbatim →
   `sample_truncated.itch`. From the project root:

   ```python
   import struct
   from pathlib import Path

   src = Path("data/01302019.NASDAQ_ITCH50.prefix.itch").read_bytes()
   pos = 0
   for _ in range(500_000):
       (length,) = struct.unpack_from(">H", src, pos)
       pos += 2 + length
   Path("data/sample_truncated.itch").write_bytes(src[:pos])
   ```

   The result is 14,409,954 bytes with SHA-256
   `94ee760bb8beff19e6f32281152f88aa0395420b75fe1eeb273ab2d4755342a0`.

Steps 1–2 are the only network activity, and they move 150 MiB, not 4.4 GiB.
