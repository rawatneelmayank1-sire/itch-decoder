"""Command line interface: ``python -m itch <command>``.

Deliberately thin. Every command is a few lines of argument handling around a function
in another module, so the pipeline stays testable without going through argv.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .messages import PRICE_SCALE

DEFAULT_DB = "itch.db"
DEFAULT_RESULTS = "results"
DEFAULT_DATA = "data/01302019.NASDAQ_ITCH50.prefix.itch"


def _fmt_price(price: int | None) -> str:
    return "--" if price is None else f"{price / PRICE_SCALE:,.4f}"


def _fmt_time(ns: int) -> str:
    """Nanoseconds since midnight -> HH:MM:SS.mmm US/Eastern."""
    seconds, rem = divmod(int(ns), 1_000_000_000)
    hours, rem_s = divmod(seconds, 3600)
    minutes, secs = divmod(rem_s, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{rem // 1_000_000:03d}"


def cmd_decode(args: argparse.Namespace) -> int:
    """Decode a file, rebuild all books, cross-check, and land into SQLite."""
    from .pipeline import DEFAULT_SYMBOLS, run_pipeline

    if not Path(args.file).exists():
        print(f"no such file: {args.file}", file=sys.stderr)
        print(
            "Generate a synthetic file with `python -m itch generate` or fetch a real "
            "one with `python tools_download.py`.",
            file=sys.stderr,
        )
        return 2
    max_bytes = int(args.max_mb * 1_000_000) if args.max_mb else None
    result = run_pipeline(
        args.file,
        db_path=args.db,
        max_bytes=max_bytes,
        symbols=tuple(args.symbols) if args.symbols else DEFAULT_SYMBOLS,
        snapshot_interval_ns=int(args.snapshot_seconds * 1_000_000_000),
        source_kind=args.kind,
    )
    print()
    print(f"run_id           {result.run_id}")
    print(f"bytes            {result.bytes_read:,}")
    print(f"messages         {result.messages:,}")
    print(f"book events      {result.book_events:,}")
    print(f"snapshots        {result.snapshots:,}")
    print(f"decode           {result.decode_seconds:.2f}s")
    print(f"replay           {result.replay_seconds:.2f}s")
    return 0


def cmd_book(args: argparse.Namespace) -> int:
    """Rebuild one symbol's book and print its state (and optionally a depth ladder)."""
    from .decoder import read_file
    from .fast_decoder import book_events, decode_columnar, symbol_directory
    from .replay import replay

    data = read_file(args.file, max_bytes=int(args.max_mb * 1_000_000) if args.max_mb else None)
    batch = decode_columnar(data)
    directory = symbol_directory(batch)
    locate = directory.get(args.symbol.upper())
    if locate is None:
        print(f"symbol {args.symbol!r} not in the stock directory", file=sys.stderr)
        return 2

    # Filtering to one locate before replay is the whole trick: it turns a
    # whole-market reconstruction into a single-symbol one.
    events = book_events(batch, stock_locate=locate)
    result = replay(events, check_oracle=True)
    book = result.multibook.book(locate)
    bids, asks = book.depth(args.levels)

    print(f"{args.symbol.upper()}  (stock_locate {locate})")
    print(f"events applied   {result.messages_applied:,}")
    if events.size:
        print(f"last timestamp   {_fmt_time(int(events['timestamp'][-1]))} US/Eastern")
    print()
    print(f"{'bid qty':>12}  {'bid':>10}  |  {'ask':<10}  {'ask qty':<12}")
    print(f"{'-' * 12}  {'-' * 10}  |  {'-' * 10}  {'-' * 12}")
    for i in range(max(len(bids), len(asks))):
        bq, bp = (f"{bids[i][1]:,}", _fmt_price(bids[i][0])) if i < len(bids) else ("", "")
        ap, aq = (_fmt_price(asks[i][0]), f"{asks[i][1]:,}") if i < len(asks) else ("", "")
        print(f"{bq:>12}  {bp:>10}  |  {ap:<10}  {aq:<12}")
    print()
    spread = book.spread()
    print(f"best bid         {_fmt_price(book.best_bid())}")
    print(f"best ask         {_fmt_price(book.best_ask())}")
    print(f"spread           {_fmt_price(spread)}")
    imbalance = book.imbalance(args.levels)
    print(f"imbalance        {'--' if imbalance is None else f'{imbalance:+.4f}'}")
    print(f"live orders      {book.total_orders():,}")
    print(f"price levels     {len(book.bids):,} bid / {len(book.asks):,} ask")
    print()
    oracle = result.oracle
    print("-- correctness oracle (book vs the exchange's own execution reports) --")
    print(f"executions       {oracle.executions_checked:,}")
    print(f"refs resolved    {oracle.refs_resolved:,}  ({oracle.ref_resolution_rate:.4%})")
    print(f"at touch         {oracle.at_touch:,}  ({oracle.agreement_rate:.4%})")
    print(f"away from touch  {oracle.away_from_touch:,}")
    print(f"side empty       {oracle.side_empty:,}")
    book.check_invariants()
    print("invariants       OK")
    return 0


def cmd_analyse(args: argparse.Namespace) -> int:
    """Run pandas analysis over the landed database and write charts."""
    from .analyse import run_analysis

    if not Path(args.db).exists():
        print(f"no database at {args.db}; run `python -m itch decode` first", file=sys.stderr)
        return 2
    summary = run_analysis(args.db, args.results, args.run_id)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """Run the benchmark matrix and write results/benchmarks.{md,json}."""
    from .bench import run_benchmarks

    payload = run_benchmarks(
        real_file=args.file if Path(args.file).exists() else None,
        results_dir=args.results,
        trials=args.trials,
    )
    print(Path(args.results, "benchmarks.md").read_text())
    return 0 if payload["results"] else 1


def cmd_generate(args: argparse.Namespace) -> int:
    """Write a synthetic, spec-conformant ITCH file."""
    from .generator import write_fixture

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    size = write_fixture(args.out, args.messages, seed=args.seed)
    print(f"wrote {size:,} bytes ({args.messages:,} order-flow messages) to {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m itch",
        description="NASDAQ ITCH 5.0 decoder, order book reconstructor, and analytics.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_decode = sub.add_parser("decode", help="decode a file and land results in SQLite")
    p_decode.add_argument("file", nargs="?", default=DEFAULT_DATA)
    p_decode.add_argument("--db", default=DEFAULT_DB)
    p_decode.add_argument("--max-mb", type=float, default=None, help="cap bytes read")
    p_decode.add_argument("--symbols", nargs="*", default=None)
    p_decode.add_argument("--snapshot-seconds", type=float, default=1.0)
    p_decode.add_argument("--kind", choices=("real", "synthetic"), default="real")
    p_decode.set_defaults(func=cmd_decode)

    p_book = sub.add_parser("book", help="rebuild and print one symbol's book")
    p_book.add_argument("symbol")
    p_book.add_argument("--file", default=DEFAULT_DATA)
    p_book.add_argument("--max-mb", type=float, default=None)
    p_book.add_argument("--levels", type=int, default=10)
    p_book.set_defaults(func=cmd_book)

    p_analyse = sub.add_parser("analyse", help="analyse the database and write charts")
    p_analyse.add_argument("--db", default=DEFAULT_DB)
    p_analyse.add_argument("--results", default=DEFAULT_RESULTS)
    p_analyse.add_argument("--run-id", type=int, default=None)
    p_analyse.set_defaults(func=cmd_analyse)

    p_bench = sub.add_parser("bench", help="benchmark naive vs vectorised decoding")
    p_bench.add_argument("--file", default=DEFAULT_DATA)
    p_bench.add_argument("--results", default=DEFAULT_RESULTS)
    p_bench.add_argument("--trials", type=int, default=5)
    p_bench.set_defaults(func=cmd_bench)

    p_gen = sub.add_parser("generate", help="write a synthetic ITCH 5.0 file")
    p_gen.add_argument("out")
    p_gen.add_argument("--messages", type=int, default=100_000)
    p_gen.add_argument("--seed", type=int, default=20190130)
    p_gen.set_defaults(func=cmd_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
