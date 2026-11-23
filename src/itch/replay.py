"""Stream replay, periodic snapshots, and the book-vs-exchange correctness oracle.

**The oracle is the point of this module.** Anyone can write code that consumes ITCH
messages and produces a data structure that looks like a book. The hard question is
whether the book is *right*, and a delta feed gives you no state message to compare
against.

But the exchange does tell you something. It transmits its own Order Executed (E, C)
messages, and those are statements about the exchange's book, not ours. Each one is a
falsifiable prediction we can test:

1. **Reference resolution.** An execution names an order reference. If we do not have
   that reference resting in our book, we missed or mishandled its Add. Because the
   data used here starts at the true beginning of the session (system event ``O``),
   there is no warm-up excuse: every reference should resolve.

2. **Price-priority agreement.** Nasdaq matches with strict price priority, so an
   execution against a *displayed* buy order resting at price P proves no better-priced
   displayed bid existed at that instant. Our book must therefore report ``best_bid ==
   P`` immediately before we apply the message. If it says otherwise, our book is
   provably wrong. This is the strongest check available and the headline number.

3. **Quantity feasibility.** An execution cannot remove more shares than were resting.

4. **Non-crossing.** A single venue's displayed book cannot have bid >= ask.

Each is checked *before* the message is applied, against the state the exchange itself
was looking at when it generated the message.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from .book import MultiBook
from .messages import BUY

#: Nanoseconds in one second. Timestamps are ns since midnight US/Eastern.
NS_PER_SECOND = 1_000_000_000


@dataclass(slots=True)
class OracleReport:
    """Falsification counts from cross-checking the book against exchange messages."""

    executions_checked: int = 0
    refs_resolved: int = 0
    at_touch: int = 0
    """Executions where our best price on the resting side equalled the order's price."""
    away_from_touch: int = 0
    side_empty: int = 0
    """Resting side had no live level at all -- a strictly worse failure than 'away'."""
    quantity_feasible: int = 0
    crossed_snapshots: int = 0
    snapshots_taken: int = 0

    @property
    def ref_resolution_rate(self) -> float:
        """Share of executions whose order reference we had resting."""
        if self.executions_checked == 0:
            return float("nan")
        return self.refs_resolved / self.executions_checked

    @property
    def agreement_rate(self) -> float:
        """Headline: share of *resolved* executions that occurred at our touch price."""
        if self.refs_resolved == 0:
            return float("nan")
        return self.at_touch / self.refs_resolved

    @property
    def quantity_feasible_rate(self) -> float:
        if self.refs_resolved == 0:
            return float("nan")
        return self.quantity_feasible / self.refs_resolved

    def as_dict(self) -> dict[str, float | int]:
        return {
            "executions_checked": self.executions_checked,
            "refs_resolved": self.refs_resolved,
            "ref_resolution_rate": self.ref_resolution_rate,
            "at_touch": self.at_touch,
            "away_from_touch": self.away_from_touch,
            "side_empty": self.side_empty,
            "agreement_rate": self.agreement_rate,
            "quantity_feasible": self.quantity_feasible,
            "quantity_feasible_rate": self.quantity_feasible_rate,
            "crossed_snapshots": self.crossed_snapshots,
            "snapshots_taken": self.snapshots_taken,
        }


@dataclass(slots=True)
class Snapshot:
    """Top-of-book plus depth for one symbol at one instant. All prices fixed-point."""

    stock_locate: int
    timestamp: int
    bid: int | None
    ask: int | None
    bid_qty: int
    ask_qty: int
    bid_levels: int
    ask_levels: int
    depth_bid_qty: int
    depth_ask_qty: int
    imbalance: float | None

    @property
    def spread(self) -> int | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(slots=True)
class ReplayResult:
    """Everything a replay produces."""

    multibook: MultiBook
    oracle: OracleReport
    snapshots: list[Snapshot] = field(default_factory=list)
    messages_applied: int = 0


def replay(
    events: np.ndarray,
    *,
    track_locates: Sequence[int] | None = None,
    snapshot_interval_ns: int = NS_PER_SECOND,
    depth_levels: int = 5,
    check_oracle: bool = True,
    strict: bool = False,
    progress: Callable[[int], None] | None = None,
) -> ReplayResult:
    """Apply a stream-ordered array of book events, snapshotting on a fixed schedule.

    ``events`` must be sorted by ``seq`` -- see :func:`itch.fast_decoder.book_events`.
    Order matters absolutely: a stateful replay of a delta feed has exactly one correct
    interleaving.

    ``track_locates`` selects which symbols get periodic snapshots. Snapshotting all
    ~8,700 listed symbols every second would produce tens of millions of rows and
    dominate runtime; a handful of names is what any real analysis wants.
    """
    multibook = MultiBook(strict=strict)
    oracle = OracleReport()
    snapshots: list[Snapshot] = []
    tracked = set(track_locates or ())
    next_snapshot: dict[int, int] = {}

    # Column extraction, then zip. Row-wise indexing into a NumPy structured array
    # builds a scalar record object per access; pulling columns out to Python lists
    # once is several times faster over millions of rows.
    types = events["msg_type"].tolist()
    timestamps = events["timestamp"].tolist()
    locates = events["stock_locate"].tolist()
    refs = events["order_ref"].tolist()
    new_refs = events["new_order_ref"].tolist()
    sides = events["side"].tolist()
    shares_col = events["shares"].tolist()
    prices = events["price"].tolist()
    printables = events["printable"].tolist()

    applied = 0
    for i in range(len(types)):
        mtype = types[i]
        locate = locates[i]
        timestamp = timestamps[i]

        if mtype == b"A" or mtype == b"F":
            multibook.on_add(
                locate, refs[i], sides[i], prices[i], shares_col[i], timestamp
            )
        elif mtype == b"E" or mtype == b"C":
            ref = refs[i]
            qty = shares_col[i]
            explicit_price = prices[i] if mtype == b"C" else None
            if check_oracle:
                _check_execution(multibook, oracle, ref, qty)
            multibook.on_execute(
                ref,
                qty,
                explicit_price,
                printable=(mtype != b"C" or printables[i] != b"N"),
            )
        elif mtype == b"X":
            multibook.on_cancel(refs[i], shares_col[i])
        elif mtype == b"D":
            multibook.on_delete(refs[i])
        elif mtype == b"U":
            multibook.on_replace(
                refs[i], new_refs[i], shares_col[i], prices[i], timestamp
            )
        elif mtype == b"P":
            # Non-displayed execution: volume only, never a book mutation.
            multibook.on_trade(shares_col[i], prices[i])
        applied += 1

        if tracked and locate in tracked:
            due = next_snapshot.get(locate)
            if due is None or timestamp >= due:
                snapshots.append(
                    _snapshot(multibook, locate, timestamp, depth_levels)
                )
                oracle.snapshots_taken += 1
                if multibook.book(locate).is_crossed():
                    oracle.crossed_snapshots += 1
                next_snapshot[locate] = timestamp + snapshot_interval_ns

        if progress is not None and (applied & 0xFFFFF) == 0:
            progress(applied)

    return ReplayResult(
        multibook=multibook,
        oracle=oracle,
        snapshots=snapshots,
        messages_applied=applied,
    )


def _check_execution(
    multibook: MultiBook, oracle: OracleReport, ref: int, qty: int
) -> None:
    """Falsify the book against one exchange execution report, before applying it."""
    oracle.executions_checked += 1
    node = multibook.order_index.get(ref)
    if node is None:
        return
    locate = multibook.locate_of_ref.get(ref)
    if locate is None:
        return
    oracle.refs_resolved += 1
    if qty <= node.shares:
        oracle.quantity_feasible += 1
    book = multibook.book(locate)
    # Strict price priority: an execution against a displayed order proves that order
    # was at the touch on its side.
    touch = book.best_bid() if node.side == BUY else book.best_ask()
    if touch is None:
        oracle.side_empty += 1
    elif touch == node.price:
        oracle.at_touch += 1
    else:
        oracle.away_from_touch += 1


def _snapshot(
    multibook: MultiBook, locate: int, timestamp: int, depth_levels: int
) -> Snapshot:
    book = multibook.book(locate)
    bids, asks = book.depth(depth_levels)
    bid = bids[0][0] if bids else None
    ask = asks[0][0] if asks else None
    depth_bid = sum(q for _p, q in bids)
    depth_ask = sum(q for _p, q in asks)
    total = depth_bid + depth_ask
    return Snapshot(
        stock_locate=locate,
        timestamp=timestamp,
        bid=bid,
        ask=ask,
        bid_qty=bids[0][1] if bids else 0,
        ask_qty=asks[0][1] if asks else 0,
        bid_levels=len(book.bids),
        ask_levels=len(book.asks),
        depth_bid_qty=depth_bid,
        depth_ask_qty=depth_ask,
        imbalance=((depth_bid - depth_ask) / total) if total else None,
    )
