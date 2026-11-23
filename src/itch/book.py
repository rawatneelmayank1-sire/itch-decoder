"""Limit order book reconstruction from an ITCH 5.0 message stream.

Why this file has to exist at all: **the exchange transmits changes, not state.** ITCH
is a delta feed. There is no message that says "the book for AAPL is now X". There is
"order 9005 added, 600 shares, $107.26", then later "order 9005 executed 100 shares".
If you want to know the spread at 10:31:04.212, you must have applied every one of the
preceding messages in order. Book reconstruction *is* the product; the decoder is
plumbing.

Data structure choices, which are the substance of this module:

* **Price levels live in a ``dict[int, PriceLevel]``**, not a sorted container. Adds and
  cancels are overwhelmingly the most common operations and both are keyed by exact
  price, so hashing is the right access path: O(1). Prices are integers (see
  :mod:`itch.messages`) so the hash is exact.

* **Each ``PriceLevel`` owns an intrusive doubly-linked list of orders.** This is the
  load-bearing decision. Cancels and deletes arrive by *order reference number*, not by
  position, and they are the majority of the feed -- in the real 2019-01-30 slice used
  here, D + X messages outnumber A messages. With a ``list`` or a ``deque``, removing an
  arbitrary element is O(n) in the level's depth: ``deque`` gives O(1) only at the two
  ends, and ``remove()`` is a linear scan. The linked list is *intrusive* -- the ``prev``
  and ``next`` pointers live on the order node itself -- so ``order_index[ref]`` hands
  back the node and unlinking is four pointer writes. Genuine O(1) cancel, independent
  of how deep the level is.

* **Best bid/ask come from heaps with lazy deletion.** Emptying a level does not remove
  its price from the heap; stale entries are discarded when they surface. See
  :meth:`OrderBook.best_bid` for the amortisation argument.

* **Execute/Cancel/Delete messages do not carry the ticker.** Only the order reference.
  So the ``order_index`` must be shared across every symbol's book -- that is why
  :class:`MultiBook` threads one dict through all of them, and why a per-symbol index
  would simply not work.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from dataclasses import dataclass, field

from .messages import BUY


class BookError(Exception):
    """Base for order book consistency failures."""


class CrossedBookError(BookError):
    """Raised in strict mode when the reconstructed book crosses (bid >= ask)."""


class OrderNode:
    """One live order, and simultaneously a node in its price level's linked list.

    "Intrusive" means the list pointers are fields of the payload object rather than
    of a separate wrapper. That saves an allocation and an indirection per order, and
    -- the real point -- lets ``order_index[ref]`` return something that can be
    unlinked immediately, with no search.

    ``__slots__`` is not micro-optimisation here: at a few million live orders the
    per-instance ``__dict__`` would dominate resident memory.
    """

    __slots__ = ("level", "next", "prev", "price", "ref", "shares", "side", "timestamp")

    def __init__(
        self, ref: int, side: bytes, price: int, shares: int, timestamp: int = 0
    ) -> None:
        self.ref = ref
        self.side = side
        self.price = price
        self.shares = shares
        self.timestamp = timestamp
        self.level: PriceLevel | None = None
        self.prev: OrderNode | None = None
        self.next: OrderNode | None = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        side = self.side.decode()
        return f"OrderNode(ref={self.ref}, {side} {self.shares}@{self.price})"


class PriceLevel:
    """All resting orders at one price, in strict time priority.

    ``head`` is the oldest order (first to be filled), ``tail`` the newest. Adds append
    at the tail; the exchange's matching engine takes from the head. ``total_qty`` is
    maintained incrementally so depth queries never walk the list -- the property tests
    assert it equals the sum of the node quantities after every operation, because an
    incrementally maintained aggregate that drifts is the classic silent book bug.
    """

    __slots__ = ("head", "order_count", "price", "tail", "total_qty")

    def __init__(self, price: int) -> None:
        self.price = price
        self.total_qty = 0
        self.order_count = 0
        self.head: OrderNode | None = None
        self.tail: OrderNode | None = None

    def append(self, node: OrderNode) -> None:
        """Add at the back of the queue. New orders always lose to resting ones."""
        node.level = self
        node.prev = self.tail
        node.next = None
        if self.tail is None:
            self.head = node
        else:
            self.tail.next = node
        self.tail = node
        self.total_qty += node.shares
        self.order_count += 1

    def unlink(self, node: OrderNode) -> None:
        """Remove ``node`` in O(1). Four pointer writes, no scan."""
        if node.prev is None:
            self.head = node.next
        else:
            node.prev.next = node.next
        if node.next is None:
            self.tail = node.prev
        else:
            node.next.prev = node.prev
        node.prev = node.next = None
        node.level = None
        self.total_qty -= node.shares
        self.order_count -= 1

    def orders(self) -> Iterator[OrderNode]:
        """Iterate the queue head-first, i.e. in execution order."""
        node = self.head
        while node is not None:
            yield node
            node = node.next

    def __len__(self) -> int:
        return self.order_count

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PriceLevel({self.price}, qty={self.total_qty}, n={self.order_count})"


@dataclass(slots=True)
class BookStats:
    """Counters that make the reconstruction auditable rather than merely plausible."""

    adds: int = 0
    executes: int = 0
    executes_with_price: int = 0
    cancels: int = 0
    deletes: int = 0
    replaces: int = 0
    trades_non_displayed: int = 0

    unknown_ref_executes: int = 0
    unknown_ref_cancels: int = 0
    unknown_ref_deletes: int = 0
    unknown_ref_replaces: int = 0
    overfilled_executes: int = 0
    """Executions claiming more shares than the order was resting with."""

    crossed_events: int = 0
    executed_volume: int = 0
    executed_notional: int = 0
    """Sum of price * shares in fixed point. Integer, so exact."""


class OrderBook:
    """Single-symbol limit order book.

    All prices are fixed-point integers. All quantities are shares.

    Complexity (n = price levels on a side, d = orders at one level):

    ==================  ==========================================================
    add_order           O(1) amortised (dict insert + tail append + heap push)
    cancel / delete     O(1)  -- intrusive unlink via order_index
    execute             O(1)
    replace             O(1)  -- delete + add
    best_bid/best_ask   O(1) amortised over the run; O(log n) per stale pop
    depth(k)            O(n log n) -- sorts the live prices; use sparingly
    ==================  ==========================================================
    """

    __slots__ = ("_ask_heap", "_bid_heap", "asks", "bids", "orders", "strict", "symbol")

    def __init__(
        self,
        symbol: str = "",
        *,
        order_index: dict[int, OrderNode] | None = None,
        strict: bool = False,
    ) -> None:
        self.symbol = symbol
        self.bids: dict[int, PriceLevel] = {}
        self.asks: dict[int, PriceLevel] = {}
        #: Shared with sibling books when driven by :class:`MultiBook`, because
        #: E/C/X/D/U messages identify orders only by reference number.
        self.orders: dict[int, OrderNode] = {} if order_index is None else order_index
        # Bids use negated prices so heapq's min-heap behaves as a max-heap.
        self._bid_heap: list[int] = []
        self._ask_heap: list[int] = []
        self.strict = strict

    # ---------------------------------------------------------------- mutations

    def add_order(
        self, ref: int, side: bytes, price: int, shares: int, timestamp: int = 0
    ) -> OrderNode:
        """Insert a new displayed order at the back of its price level's queue."""
        node = OrderNode(ref, side, price, shares, timestamp)
        levels, heap = (
            (self.bids, self._bid_heap) if side == BUY else (self.asks, self._ask_heap)
        )
        level = levels.get(price)
        if level is None:
            level = PriceLevel(price)
            levels[price] = level
            # Push only on level creation. A price destroyed and recreated is pushed
            # again, which is fine: pushes are still bounded by the number of
            # operations, which is what the amortisation argument needs.
            heapq.heappush(heap, -price if side == BUY else price)
        level.append(node)
        self.orders[ref] = node
        return node

    def reduce(self, node: OrderNode, shares: int) -> int:
        """Take ``shares`` off a resting order; remove it if fully consumed.

        Returns the quantity actually removed, which is clamped to what was resting.
        Clamping rather than trusting the message is deliberate: if our book and the
        exchange disagree, silently going negative would corrupt every later depth
        figure. The discrepancy is counted by the caller instead.
        """
        taken = min(shares, node.shares)
        level = node.level
        assert level is not None, "order node is not linked into a level"
        if taken == node.shares:
            level.unlink(node)
            self.orders.pop(node.ref, None)
            self._drop_level_if_empty(level, node.side)
        else:
            node.shares -= taken
            level.total_qty -= taken
        return taken

    def remove(self, node: OrderNode) -> None:
        """Delete a resting order outright (ITCH ``D``)."""
        level = node.level
        assert level is not None, "order node is not linked into a level"
        side = node.side
        level.unlink(node)
        self.orders.pop(node.ref, None)
        self._drop_level_if_empty(level, side)

    def _drop_level_if_empty(self, level: PriceLevel, side: bytes) -> None:
        """Remove an emptied level from the dict but *leave its heap entry alone*.

        Removing from the middle of a binary heap is O(n) (find) plus O(log n) (sift).
        Leaving the entry to be discarded on the next best-price query is strictly
        cheaper -- see :meth:`best_bid`.
        """
        if level.order_count == 0:
            levels = self.bids if side == BUY else self.asks
            levels.pop(level.price, None)

    # ---------------------------------------------------------------- queries

    def best_bid(self) -> int | None:
        """Highest live bid price, or ``None``.

        Lazy deletion: pop from the heap until the top is a price that still has a live
        level. Amortised argument -- each price is pushed at most once per level
        creation, so over *m* operations there are at most *m* pushes and therefore at
        most *m* pops across the entire run. Total heap work is O(m log m), i.e.
        **O(log n) amortised per operation**. A single call can pop many stale entries,
        but that work was already paid for by the pushes that created them; it is not
        repeatable, so there is no adversarial input that makes the run quadratic.
        """
        heap = self._bid_heap
        bids = self.bids
        while heap:
            price = -heap[0]
            if price in bids:
                return price
            heapq.heappop(heap)
        return None

    def best_ask(self) -> int | None:
        """Lowest live ask price, or ``None``. Same amortisation as :meth:`best_bid`."""
        heap = self._ask_heap
        asks = self.asks
        while heap:
            price = heap[0]
            if price in asks:
                return price
            heapq.heappop(heap)
        return None

    def spread(self) -> int | None:
        """Best ask minus best bid, in fixed point. ``None`` if either side is empty."""
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def mid(self) -> float | None:
        """Midpoint in fixed-point units. Fractional, hence float -- display only."""
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    def is_crossed(self) -> bool:
        """True when the best bid is at or above the best ask.

        A single venue's *displayed* book must never cross: an order that would cross
        executes instead of resting. So a crossed reconstruction is proof of a bug --
        which is precisely why this is checked rather than assumed.
        """
        bid, ask = self.best_bid(), self.best_ask()
        return bid is not None and ask is not None and bid >= ask

    def depth(self, levels: int = 5) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """Top ``levels`` of each side as ``(price, total_qty)``, best first.

        O(n log n) in the number of live levels because it sorts. Fine for periodic
        snapshots, wrong for a per-message hot loop -- use :meth:`best_bid` there.
        """
        bid_prices = sorted(self.bids, reverse=True)[:levels]
        ask_prices = sorted(self.asks)[:levels]
        return (
            [(p, self.bids[p].total_qty) for p in bid_prices],
            [(p, self.asks[p].total_qty) for p in ask_prices],
        )

    def imbalance(self, levels: int = 5) -> float | None:
        """Order book imbalance in [-1, 1]: (bid_qty - ask_qty) / (bid_qty + ask_qty).

        Positive means more size resting on the bid. A standard microstructure feature
        and a decent short-horizon predictor of the direction of the next trade.
        """
        bids, asks = self.depth(levels)
        bid_qty = sum(q for _p, q in bids)
        ask_qty = sum(q for _p, q in asks)
        total = bid_qty + ask_qty
        if total == 0:
            return None
        return (bid_qty - ask_qty) / total

    def total_orders(self) -> int:
        """Live order count in this book (walks levels, so O(levels))."""
        return sum(lvl.order_count for lvl in self.bids.values()) + sum(
            lvl.order_count for lvl in self.asks.values()
        )

    def check_invariants(self) -> None:
        """Assert every structural invariant. Used by the property-based tests.

        Deliberately exhaustive and deliberately slow -- it walks every list. It is the
        specification of "the book is not corrupt", written as code.
        """
        for side_name, levels in (("bid", self.bids), ("ask", self.asks)):
            for price, level in levels.items():
                if level.price != price:
                    raise BookError(f"{side_name} level keyed {price} holds {level.price}")
                if level.order_count == 0:
                    raise BookError(f"empty {side_name} level {price} left in dict")
                walked, qty, seen = 0, 0, []
                node = level.head
                prev = None
                while node is not None:
                    if node.prev is not prev:
                        raise BookError(f"broken prev pointer at ref {node.ref}")
                    if node.level is not level:
                        raise BookError(f"node {node.ref} points at the wrong level")
                    if node.price != price:
                        raise BookError(f"node {node.ref} price {node.price} != {price}")
                    if node.shares <= 0:
                        raise BookError(f"node {node.ref} has non-positive shares")
                    seen.append(node.ref)
                    qty += node.shares
                    walked += 1
                    prev, node = node, node.next
                if level.tail is not prev:
                    raise BookError(f"tail pointer wrong on {side_name} level {price}")
                if walked != level.order_count:
                    raise BookError(
                        f"{side_name} level {price}: order_count={level.order_count} "
                        f"but list holds {walked}"
                    )
                if qty != level.total_qty:
                    raise BookError(
                        f"{side_name} level {price}: total_qty={level.total_qty} "
                        f"but orders sum to {qty}"
                    )
                for ref in seen:
                    if self.orders.get(ref) is None:
                        raise BookError(f"ref {ref} is in a level but not in order_index")
        if self.is_crossed():
            raise CrossedBookError(
                f"book crossed: bid {self.best_bid()} >= ask {self.best_ask()}"
            )


@dataclass(slots=True)
class MultiBook:
    """Books for every symbol, driven by a stream of decoded ITCH events.

    The shared ``order_index`` is the crux: an Order Executed message carries an order
    reference and nothing else -- no ticker, no side, no price. The only way to know
    which book it belongs to is to have remembered where that reference was added.
    """

    strict: bool = False
    books: dict[int, OrderBook] = field(default_factory=dict)
    order_index: dict[int, OrderNode] = field(default_factory=dict)
    locate_of_ref: dict[int, int] = field(default_factory=dict)
    stats: BookStats = field(default_factory=BookStats)

    def book(self, stock_locate: int) -> OrderBook:
        """Get or create the book for a locate code."""
        existing = self.books.get(stock_locate)
        if existing is None:
            existing = OrderBook(
                symbol=str(stock_locate),
                order_index=self.order_index,
                strict=self.strict,
            )
            self.books[stock_locate] = existing
        return existing

    # -------------------------------------------------------------- message handlers

    def on_add(
        self,
        stock_locate: int,
        ref: int,
        side: bytes,
        price: int,
        shares: int,
        timestamp: int = 0,
    ) -> None:
        """ITCH A / F. F differs only by carrying an MPID, which does not affect state."""
        book = self.book(stock_locate)
        book.add_order(ref, side, price, shares, timestamp)
        self.locate_of_ref[ref] = stock_locate
        self.stats.adds += 1
        if book.is_crossed():
            self.stats.crossed_events += 1
            if self.strict:
                raise CrossedBookError(
                    f"add of {ref} crossed locate {stock_locate}: "
                    f"bid {book.best_bid()} >= ask {book.best_ask()}"
                )

    def on_execute(
        self, ref: int, shares: int, price: int | None = None, printable: bool = True
    ) -> tuple[int, int] | None:
        """ITCH E / C. Returns ``(execution_price, quantity)`` or None if ref unknown.

        ``C`` (Order Executed With Price) carries an explicit price because the trade
        did not print at the order's display price. Its ``printable`` flag says whether
        the execution should be included in volume/consolidated tape statistics -- a
        non-printable C still consumes book liquidity but must not be counted as
        volume, so the two effects are handled separately.
        """
        node = self.order_index.get(ref)
        if node is None:
            self.stats.unknown_ref_executes += 1
            return None
        locate = self.locate_of_ref.get(ref)
        book = self.book(locate) if locate is not None else None
        if book is None:
            self.stats.unknown_ref_executes += 1
            return None
        exec_price = node.price if price is None else price
        if shares > node.shares:
            self.stats.overfilled_executes += 1
        taken = book.reduce(node, shares)
        if price is None:
            self.stats.executes += 1
        else:
            self.stats.executes_with_price += 1
        if printable:
            self.stats.executed_volume += taken
            self.stats.executed_notional += exec_price * taken
        if taken == 0 or ref not in self.order_index:
            self.locate_of_ref.pop(ref, None)
        return exec_price, taken

    def on_cancel(self, ref: int, shares: int) -> None:
        """ITCH X: partial cancel. The order stays if quantity remains."""
        node = self.order_index.get(ref)
        if node is None:
            self.stats.unknown_ref_cancels += 1
            return
        locate = self.locate_of_ref.get(ref)
        if locate is None:
            self.stats.unknown_ref_cancels += 1
            return
        self.book(locate).reduce(node, shares)
        self.stats.cancels += 1
        if ref not in self.order_index:
            self.locate_of_ref.pop(ref, None)

    def on_delete(self, ref: int) -> None:
        """ITCH D: the order is gone entirely."""
        node = self.order_index.get(ref)
        if node is None:
            self.stats.unknown_ref_deletes += 1
            return
        locate = self.locate_of_ref.pop(ref, None)
        if locate is None:
            self.stats.unknown_ref_deletes += 1
            return
        self.book(locate).remove(node)
        self.stats.deletes += 1

    def on_replace(
        self, original_ref: int, new_ref: int, shares: int, price: int, timestamp: int = 0
    ) -> None:
        """ITCH U: cancel-and-replace, atomically.

        Semantically this is **delete + add**, and the replacement goes to the *back* of
        the queue at its new price. That is not an implementation shortcut, it is how
        the exchange behaves: queue position is earned by resting, and any change to
        price or an increase in size forfeits it. Modelling a replace as an in-place
        edit that keeps priority would make every queue-position estimate downstream
        optimistic -- you would think you were third in line when you were thirtieth.

        A replace also mints a *new* order reference; the original is dead and must
        never be resolvable again, or a later stale cancel would silently hit the wrong
        order.
        """
        node = self.order_index.get(original_ref)
        if node is None:
            self.stats.unknown_ref_replaces += 1
            return
        locate = self.locate_of_ref.pop(original_ref, None)
        if locate is None:
            self.stats.unknown_ref_replaces += 1
            return
        book = self.book(locate)
        side = node.side
        book.remove(node)
        book.add_order(new_ref, side, price, shares, timestamp)
        self.locate_of_ref[new_ref] = locate
        self.stats.replaces += 1
        if book.is_crossed():
            self.stats.crossed_events += 1
            if self.strict:
                raise CrossedBookError(
                    f"replace {original_ref}->{new_ref} crossed locate {locate}"
                )

    def on_trade(self, shares: int, price: int) -> None:
        """ITCH P: execution of a **non-displayable** order.

        Critically, this must NOT touch the book. A P message reports a trade against
        hidden liquidity that was never in the displayed book in the first place, so
        removing anything on its account would delete a real displayed order that is
        still live. Getting this wrong is a classic reconstruction bug: the book slowly
        drains and the spread widens for no reason. P messages count toward volume only.
        """
        self.stats.trades_non_displayed += 1
        self.stats.executed_volume += shares
        self.stats.executed_notional += price * shares

    def check_invariants(self) -> None:
        """Run every book's invariant check. O(total live orders)."""
        for book in self.books.values():
            book.check_invariants()
