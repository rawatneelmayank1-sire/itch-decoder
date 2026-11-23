"""Export a small, committable snapshot of the real decode for the static web viewer.

The source data is a 409 MB NASDAQ ITCH file that is gitignored and must never be
committed. This script reads it through the ordinary ``itch`` package -- the same
decoder, the same ``MultiBook``, the same event ordering the test suite covers -- and
writes a few megabytes of derived state into ``web/data/`` that GitHub Pages can serve
with no backend.

Three ideas keep the output small without inventing anything:

1. **A uniform time grid, sampled sparsely.** Book state is sampled onto a fixed
   ``--grid`` second grid across the session. A frame is emitted only when the visible
   ladder actually differs from the previous frame; the browser forward-fills. In a
   pre-market session most grid slots are unchanged, so this collapses hard while the
   time axis stays honest and linear.

2. **Relative integer prices.** Prices stay fixed-point integers all the way out, as
   they do in the book and in SQLite. Each frame stores its best price, then the other
   levels as offsets from it, divided by the largest exact common divisor observed for
   that symbol (``punit``, virtually always 100 = one cent). Lossless; roughly halves
   the digits.

3. **Columnar flat arrays.** ``[[a,b],[c,d]]`` spends a third of its bytes on brackets.
   Fixed-width flat arrays with a known stride do not.

Everything else on the page -- oracle counts, message-type distribution, throughput --
is copied verbatim out of ``results/pipeline_summary.json`` and ``results/benchmarks.json``,
which are produced by ``python run_all.py``. This script never recomputes or rounds them.

Output is JavaScript, not JSON, so the page also works when opened straight off disk:
``fetch()`` is blocked for ``file://`` origins, ``<script src>`` is not.

Usage
-----
    python scripts/export_web_data.py                 # resumable; skips fresh output
    python scripts/export_web_data.py --force         # rebuild everything
    python scripts/export_web_data.py --symbols SPY QQQ
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from math import gcd
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402  (after sys.path setup)

from itch.book import MultiBook  # noqa: E402
from itch.fast_decoder import book_events, decode_columnar, symbol_directory  # noqa: E402
from itch.replay import replay  # noqa: E402

REAL_ITCH = ROOT / "data" / "01302019.NASDAQ_ITCH50.prefix.itch"
RESULTS = ROOT / "results"
OUT = ROOT / "web" / "data"

#: Levels of depth kept per side. Eight is what fits a ladder on a laptop screen and is
#: deep enough that the heatmap shows liquidity building away from the touch.
LEVELS = 8

#: Symbols to export. Each is a different regime: the two biggest index ETFs, a
#: high-beta single name, and a mega-cap. AAPL is deliberately absent -- it has only 14
#: snapshot-worthy seconds in this pre-market window and would render as an empty book.
DEFAULT_SYMBOLS = ("SPY", "QQQ", "TSLA", "MSFT")

NS = 1_000_000_000


# --------------------------------------------------------------------------------------
# replay with full ladders
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Ladder:
    """One sampled book state: the top ``LEVELS`` of each side, plus level counts."""

    slot: int
    bid_px: list[int]
    bid_qty: list[int]
    ask_px: list[int]
    ask_qty: list[int]
    n_bid_levels: int
    n_ask_levels: int


def sample_ladders(
    events: np.ndarray, locate: int, *, t0: int, grid_ns: int, slots: int
) -> tuple[list[Ladder], tuple[int | None, int | None]]:
    """Replay one symbol's events, capturing a depth ladder on a fixed time grid.

    The mutation dispatch below mirrors :func:`itch.replay.replay` message for message.
    It is repeated here rather than reused because ``replay`` records only aggregate
    depth in its ``Snapshot``, and the viewer needs the ladder itself. The caller
    cross-checks the final touch against a real ``replay()`` run, so a divergence here
    is a hard failure rather than a quiet one.
    """
    mb = MultiBook(strict=False)
    out: list[Ladder] = []
    prev_key: tuple | None = None
    next_slot = 0

    types = events["msg_type"].tolist()
    timestamps = events["timestamp"].tolist()
    refs = events["order_ref"].tolist()
    new_refs = events["new_order_ref"].tolist()
    sides = events["side"].tolist()
    shares = events["shares"].tolist()
    prices = events["price"].tolist()
    printables = events["printable"].tolist()

    def capture(slot: int) -> None:
        nonlocal prev_key
        book = mb.book(locate)
        bids, asks = book.depth(LEVELS)
        key = (tuple(bids), tuple(asks))
        if key == prev_key:
            return
        prev_key = key
        out.append(
            Ladder(
                slot=slot,
                bid_px=[p for p, _ in bids],
                bid_qty=[q for _, q in bids],
                ask_px=[p for p, _ in asks],
                ask_qty=[q for _, q in asks],
                n_bid_levels=len(book.bids),
                n_ask_levels=len(book.asks),
            )
        )

    for i in range(len(types)):
        ts = timestamps[i]

        # Sample every grid slot the clock has passed, before applying this event, so a
        # frame always shows the state the exchange was looking at at that instant.
        slot = (ts - t0) // grid_ns
        if slot >= next_slot:
            capture(min(int(slot), slots - 1))
            next_slot = int(slot) + 1

        mtype = types[i]
        if mtype == b"A" or mtype == b"F":
            mb.on_add(locate, refs[i], sides[i], prices[i], shares[i], ts)
        elif mtype == b"E" or mtype == b"C":
            mb.on_execute(
                refs[i],
                shares[i],
                prices[i] if mtype == b"C" else None,
                printable=(mtype != b"C" or printables[i] != b"N"),
            )
        elif mtype == b"X":
            mb.on_cancel(refs[i], shares[i])
        elif mtype == b"D":
            mb.on_delete(refs[i])
        elif mtype == b"U":
            mb.on_replace(refs[i], new_refs[i], shares[i], prices[i], ts)
        elif mtype == b"P":
            mb.on_trade(shares[i], prices[i])

    capture(slots - 1)
    book = mb.book(locate)
    return out, (book.best_bid(), book.best_ask())


# --------------------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------------------


def price_unit(ladders: list[Ladder]) -> int:
    """Largest exact divisor of every price offset from the touch, in fixed-point units.

    Nasdaq quotes above $1.00 in pennies, so this is normally 10_000/100 = 100. It is
    measured rather than assumed, because sub-penny prices are legal below $1.00 and a
    wrong assumption here would silently corrupt every level below the touch.
    """
    g = 0
    for lad in ladders:
        for px in lad.bid_px[1:]:
            g = gcd(g, lad.bid_px[0] - px)
        for px in lad.ask_px[1:]:
            g = gcd(g, px - lad.ask_px[0])
    return g or 100


def encode_symbol(
    symbol: str,
    locate: int,
    ladders: list[Ladder],
    *,
    t0: int,
    grid_s: int,
    slots: int,
    oracle: dict,
    events_applied: int,
) -> dict:
    """Pack ladders into flat, delta-encoded integer columns."""
    punit = price_unit(ladders)

    slot_col: list[int] = []
    best_bid: list[int] = []  # fixed-point, delta-encoded frame to frame; 0 => no bid
    spread: list[int] = []  # ask - bid in fixed-point; -1 => one or both sides empty
    ask_only: list[int] = []  # best ask when there is no bid at all; 0 otherwise
    bid_off: list[int] = []  # stride LEVELS, offset below best bid, in punit
    bid_qty: list[int] = []
    ask_off: list[int] = []
    ask_qty: list[int] = []
    n_levels: list[int] = []  # stride 2

    prev_slot = 0
    prev_bid = 0
    for lad in ladders:
        slot_col.append(lad.slot - prev_slot)
        prev_slot = lad.slot

        bb = lad.bid_px[0] if lad.bid_px else 0
        ba = lad.ask_px[0] if lad.ask_px else 0
        best_bid.append(bb - prev_bid)
        prev_bid = bb
        spread.append(ba - bb if (bb and ba) else -1)
        ask_only.append(ba if (ba and not bb) else 0)

        for k in range(LEVELS):
            if k < len(lad.bid_px):
                bid_off.append((lad.bid_px[0] - lad.bid_px[k]) // punit)
                bid_qty.append(lad.bid_qty[k])
            else:
                bid_off.append(0)
                bid_qty.append(0)
        for k in range(LEVELS):
            if k < len(lad.ask_px):
                ask_off.append((lad.ask_px[k] - lad.ask_px[0]) // punit)
                ask_qty.append(lad.ask_qty[k])
            else:
                ask_off.append(0)
                ask_qty.append(0)
        n_levels.append(lad.n_bid_levels)
        n_levels.append(lad.n_ask_levels)

    return {
        "symbol": symbol,
        "stock_locate": locate,
        "t0_ns": t0,
        "grid_s": grid_s,
        "slots": slots,
        "levels": LEVELS,
        "punit": punit,
        "frames": len(ladders),
        "events_applied": events_applied,
        "oracle": oracle,
        "cols": {
            "slot_d": slot_col,
            "bid_d": best_bid,
            "spread": spread,
            "ask_only": ask_only,
            "bid_off": bid_off,
            "bid_qty": bid_qty,
            "ask_off": ask_off,
            "ask_qty": ask_qty,
            "n_levels": n_levels,
        },
    }


def write_js(path: Path, expr: str, payload: object) -> int:
    """Write ``expr = <json>;`` and return the byte size."""
    body = f"{expr} = {json.dumps(payload, separators=(',', ':'))};\n"
    path.write_text(body, encoding="utf-8")
    return len(body.encode("utf-8"))


# --------------------------------------------------------------------------------------
# summary (verbatim from the committed results)
# --------------------------------------------------------------------------------------


def build_summary(symbols: list[dict], grid_s: int, t0: int, t1: int) -> dict:
    pipeline = json.loads((RESULTS / "pipeline_summary.json").read_text())
    bench = json.loads((RESULTS / "benchmarks.json").read_text())
    analysis = json.loads((RESULTS / "analysis_summary.json").read_text())

    rows = bench["results"]
    by_key = {(r["label"], r["implementation"]): r for r in rows}

    def pair(label: str) -> dict | None:
        naive = by_key.get((label, "naive (struct.unpack per message)"))
        vec = by_key.get((label, "vectorised (NumPy structured dtype)"))
        if not naive or not vec:
            return None
        return {
            "label": label,
            "decoded": naive["decoded_messages"],
            "naive": naive["decoded_per_sec"],
            "vector": vec["decoded_per_sec"],
            "speedup": vec["decoded_per_sec"] / naive["decoded_per_sec"],
            "real": label.startswith("real"),
        }

    labels = []
    for r in rows:
        if r["label"] not in labels:
            labels.append(r["label"])
    throughput = [p for p in (pair(x) for x in labels) if p]

    return {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "file": pipeline["source_file"],
            "kind": pipeline["source_kind"],
            "feed": "Nasdaq TotalView-ITCH 5.0",
            "trading_day": "2019-01-30",
            "bytes_read": pipeline["bytes_read"],
            "messages": pipeline["messages"],
            "book_events": pipeline["book_events"],
            "snapshots": pipeline["snapshots"],
            "symbols_in_directory": pipeline["type_counts"].get("R", 0),
            "symbols_with_activity": analysis["symbols_with_activity"],
        },
        "session": {
            "t0_ns": t0,
            "t1_ns": t1,
            "grid_s": grid_s,
            "open_ns": 9 * 3600 * NS + 30 * 60 * NS,
        },
        "timing": {
            "decode_seconds": pipeline["decode_seconds"],
            "replay_seconds": pipeline["replay_seconds"],
            "decode_msgs_per_sec": pipeline["decode_msgs_per_sec"],
            "replay_msgs_per_sec": pipeline["replay_msgs_per_sec"],
        },
        "oracle": pipeline["oracle"],
        "type_counts": pipeline["type_counts"],
        "throughput": throughput,
        "profile": bench["profile_of_largest_real_decode"],
        "environment": bench["environment"],
        "analysis": {
            "median_spread_usd": analysis["median_spread_usd"],
            "median_imbalance": analysis["median_imbalance"],
            "tightest_symbols": analysis["tightest_symbols"],
            "top_symbols": analysis["top_symbols"],
        },
        "symbols": symbols,
    }


# --------------------------------------------------------------------------------------


def newer_than(target: Path, *sources: Path) -> bool:
    if not target.exists():
        return False
    tm = target.stat().st_mtime
    return all(not s.exists() or s.stat().st_mtime <= tm for s in sources)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=str(REAL_ITCH), help="ITCH file to read")
    ap.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    ap.add_argument("--grid", type=int, default=4, help="sample grid, seconds")
    ap.add_argument("--force", action="store_true", help="rebuild even if fresh")
    ap.add_argument("--max-mb", type=float, default=None)
    args = ap.parse_args(argv)

    src = Path(args.file)
    if not src.exists():
        print(
            f"source not found: {src}\n"
            "Fetch it with `python tools_download.py` (409 MB, gitignored).",
            file=sys.stderr,
        )
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    self_path = Path(__file__).resolve()
    todo = [
        s
        for s in args.symbols
        if args.force or not newer_than(OUT / f"book-{s}.js", src, self_path)
    ]
    summary_fresh = newer_than(OUT / "summary.js", src, self_path, *RESULTS.glob("*.json"))

    if not todo and summary_fresh and not args.force:
        print("web/data is newer than its inputs -- nothing to do. Use --force to rebuild.")
        return 0
    if todo:
        print(f"to build: {', '.join(todo)}")

    t_read = time.perf_counter()
    from itch.decoder import read_file

    data = read_file(str(src), max_bytes=int(args.max_mb * 1e6) if args.max_mb else None)
    print(f"read {len(data):,} bytes in {time.perf_counter() - t_read:.1f}s")

    t_dec = time.perf_counter()
    batch = decode_columnar(data)
    print(
        f"decoded {batch.total_messages:,} messages in "
        f"{time.perf_counter() - t_dec:.1f}s"
    )

    directory = symbol_directory(batch)

    # Session bounds, measured from the stream rather than assumed from the README.
    lo, hi = math.inf, -math.inf
    for mt in ("A", "F", "E", "C", "X", "D", "U", "P"):
        arr = batch.arrays.get(mt)
        if arr is None or arr.size == 0:
            continue
        ts = batch.timestamps(mt)
        lo = min(lo, int(ts.min()))
        hi = max(hi, int(ts.max()))
    t0, t1 = int(lo), int(hi)
    grid_ns = args.grid * NS
    slots = (t1 - t0) // grid_ns + 1
    print(
        f"session {t0 / NS / 3600:.4f}h -> {t1 / NS / 3600:.4f}h ET, "
        f"{slots:,} grid slots at {args.grid}s"
    )

    manifest_path = OUT / "_manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())

    for sym in args.symbols:
        locate = directory.get(sym)
        if locate is None:
            print(f"  {sym}: not in the stock directory -- skipped")
            continue
        if sym not in todo:
            print(f"  {sym}: up to date -- skipped")
            continue

        t_sym = time.perf_counter()
        events = book_events(batch, stock_locate=locate)
        ladders, final_touch = sample_ladders(
            events, locate, t0=t0, grid_ns=grid_ns, slots=slots
        )

        # Authoritative pass through the shipped replay, for this symbol's oracle
        # numbers and as a cross-check that the ladder loop above agrees with it.
        ref = replay(events, check_oracle=True)
        ref_book = ref.multibook.book(locate)
        assert final_touch == (ref_book.best_bid(), ref_book.best_ask()), (
            f"{sym}: ladder replay diverged from itch.replay.replay "
            f"{final_touch} != {(ref_book.best_bid(), ref_book.best_ask())}"
        )

        payload = encode_symbol(
            sym,
            locate,
            ladders,
            t0=t0,
            grid_s=args.grid,
            slots=slots,
            oracle=ref.oracle.as_dict(),
            events_applied=int(events.size),
        )
        size = write_js(
            OUT / f"book-{sym}.js",
            f'(window.ITCH_BOOK=window.ITCH_BOOK||{{}})["{sym}"]',
            payload,
        )
        manifest[sym] = {
            "symbol": sym,
            "stock_locate": locate,
            "frames": len(ladders),
            "events": int(events.size),
            "bytes": size,
            "executions": ref.oracle.executions_checked,
            "at_touch": ref.oracle.at_touch,
            "punit": payload["punit"],
        }
        manifest_path.write_text(json.dumps(manifest, indent=1))
        print(
            f"  {sym}: {events.size:,} events, {len(ladders):,} frames, "
            f"{size / 1024:.0f} KB, {time.perf_counter() - t_sym:.1f}s"
        )

    symbols = [manifest[s] for s in args.symbols if s in manifest]
    summary = build_summary(symbols, args.grid, t0, t1)
    ssize = write_js(OUT / "summary.js", "window.ITCH_SUMMARY", summary)
    print(f"  summary.js: {ssize / 1024:.0f} KB")

    total = sum(p.stat().st_size for p in OUT.glob("*") if p.is_file())
    print(f"web/data total: {total / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
