"""End-to-end pipeline: decode -> reconstruct -> cross-check -> land in SQLite.

Kept separate from the CLI so it is callable and testable as a function, and separate
from the analysis so the expensive half runs once and the cheap half can iterate.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import store
from .decoder import read_file
from .fast_decoder import book_events, decode_columnar, symbol_directory
from .replay import NS_PER_SECOND, replay


@dataclass(slots=True)
class PipelineResult:
    run_id: int
    bytes_read: int
    messages: int
    book_events: int
    snapshots: int
    oracle: dict[str, float | int]
    type_counts: dict[str, int]
    symbols_tracked: dict[str, int]
    decode_seconds: float
    replay_seconds: float


#: Default symbols to snapshot. Deep, liquid, and each a different regime: a mega-cap
#: tech name, an index ETF, a high-beta name, a bank. Snapshotting all ~8,700 listed
#: symbols every second would produce tens of millions of rows for no analytical gain.
DEFAULT_SYMBOLS: tuple[str, ...] = ("AAPL", "MSFT", "SPY", "QQQ", "TSLA", "AMD", "F", "BAC")


def run_pipeline(
    itch_file: str,
    *,
    db_path: str,
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    max_bytes: int | None = None,
    snapshot_interval_ns: int = NS_PER_SECOND,
    depth_levels: int = 5,
    source_kind: str = "real",
    verbose: bool = True,
) -> PipelineResult:
    """Decode a file, rebuild every symbol's book, cross-check, and land the results."""
    import time

    def say(message: str) -> None:
        if verbose:
            print(message, flush=True)

    say(f"reading {itch_file}")
    data = read_file(itch_file, max_bytes=max_bytes)
    say(f"  {len(data):,} bytes")

    start = time.perf_counter()
    batch = decode_columnar(data)
    decode_seconds = time.perf_counter() - start
    say(
        f"decoded {batch.total_messages:,} messages in {decode_seconds:.2f}s "
        f"({batch.total_messages / decode_seconds:,.0f} msg/s)"
    )

    directory = symbol_directory(batch)
    tracked = {s: directory[s] for s in symbols if s in directory}
    say(f"stock directory: {len(directory):,} symbols; tracking {len(tracked)}")

    events = book_events(batch)
    say(f"book-affecting events: {events.size:,}")

    start = time.perf_counter()
    result = replay(
        events,
        track_locates=list(tracked.values()),
        snapshot_interval_ns=snapshot_interval_ns,
        depth_levels=depth_levels,
        check_oracle=True,
    )
    replay_seconds = time.perf_counter() - start
    say(
        f"replayed {result.messages_applied:,} events in {replay_seconds:.2f}s "
        f"({result.messages_applied / replay_seconds:,.0f} msg/s)"
    )

    oracle = result.oracle.as_dict()
    say(
        f"oracle: {oracle['refs_resolved']:,}/{oracle['executions_checked']:,} refs "
        f"resolved, agreement {oracle['agreement_rate']:.4%}, "
        f"crossed snapshots {oracle['crossed_snapshots']}"
    )

    conn = store.connect(db_path)
    try:
        store.init_schema(conn)
        run_id = store.start_run(
            conn,
            source_file=str(Path(itch_file).name),
            source_kind=source_kind,
            bytes_read=len(data),
            messages=batch.total_messages,
        )
        store.insert_symbols(conn, run_id, directory)
        store.insert_message_counts(conn, run_id, batch.type_counts)
        store.insert_snapshots(conn, run_id, result.snapshots)
        store.insert_oracle(conn, run_id, oracle)
        store.insert_symbol_activity(
            conn, run_id, _symbol_activity_rows(events, directory)
        )
    finally:
        conn.close()
    say(f"landed run {run_id} into {db_path}")

    return PipelineResult(
        run_id=run_id,
        bytes_read=len(data),
        messages=batch.total_messages,
        book_events=int(events.size),
        snapshots=len(result.snapshots),
        oracle=oracle,
        type_counts=batch.type_counts,
        symbols_tracked=tracked,
        decode_seconds=decode_seconds,
        replay_seconds=replay_seconds,
    )


def _symbol_activity_rows(
    events: np.ndarray, directory: dict[str, int]
) -> list[tuple[object, ...]]:
    """Per-symbol message counts and executed volume, computed vectorised.

    Aggregating with NumPy over the event array rather than by walking the replay's
    books keeps this O(n) with no Python-level loop over millions of rows.
    """
    if events.size == 0:
        return []
    name_of = {loc: sym for sym, loc in directory.items()}
    locates = events["stock_locate"]
    types = events["msg_type"]
    shares = events["shares"].astype(np.int64)
    prices = events["price"].astype(np.int64)

    total = Counter(locates.tolist())
    adds = Counter(locates[(types == b"A") | (types == b"F")].tolist())
    exec_mask = (types == b"E") | (types == b"C") | (types == b"P")
    execs = Counter(locates[exec_mask].tolist())

    volume: dict[int, int] = {}
    notional: dict[int, int] = {}
    for loc, qty, px in zip(
        locates[exec_mask].tolist(),
        shares[exec_mask].tolist(),
        prices[exec_mask].tolist(),
        strict=True,
    ):
        volume[loc] = volume.get(loc, 0) + qty
        # E messages carry no price (the fill is at the resting order's display price),
        # so their notional is not computable from the event row alone and is omitted
        # rather than guessed. C and P rows do carry one.
        notional[loc] = notional.get(loc, 0) + px * qty

    return [
        (
            int(loc),
            name_of.get(int(loc)),
            int(n),
            int(adds.get(loc, 0)),
            int(execs.get(loc, 0)),
            int(volume.get(loc, 0)),
            int(notional.get(loc, 0)),
        )
        for loc, n in total.items()
    ]
