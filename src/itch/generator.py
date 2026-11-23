"""Byte-exact ITCH 5.0 *encoder* plus a coherent synthetic stream generator.

Two jobs, and the first is the important one:

1. :func:`encode` is an independent implementation of the wire format, written from
   the field tables rather than from the decoder. That independence is what makes
   ``encode -> decode -> compare`` a real round-trip test instead of a tautology: if I
   had derived the encoder from ``STRUCT_FORMATS`` the test would pass even with the
   offsets wrong. It builds each message by concatenating explicitly sized
   ``int.to_bytes(..., "big")`` fields, then asserts the total against
   ``MESSAGE_LENGTHS``.

2. :class:`SyntheticStream` emits a *coherent* message stream -- one where every
   execute/cancel/delete refers to a live order, executions happen at the touch, and
   the book never crosses. Real files are the primary data source for this project;
   the synthetic stream exists so tests are deterministic and hermetic (CI has no
   4.8 GB download) and so benchmarks can be run at chosen sizes.
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Any

from .messages import MESSAGE_LENGTHS, PRICE_SCALE


def _pad(text: str | bytes, width: int) -> bytes:
    """Right-pad an alpha field with spaces, as the spec requires (not NULs)."""
    raw = text.encode("ascii") if isinstance(text, str) else text
    if len(raw) > width:
        raise ValueError(f"{raw!r} exceeds {width}-byte alpha field")
    return raw.ljust(width, b" ")


def _u(value: int, width: int) -> bytes:
    """Big-endian unsigned integer of an explicit width."""
    return int(value).to_bytes(width, "big")


def _header(msg_type: str, stock_locate: int, tracking: int, timestamp: int) -> bytes:
    """The 11-byte common header. Timestamp is 48-bit, so ``to_bytes(6)`` directly."""
    return (
        msg_type.encode("ascii")
        + _u(stock_locate, 2)
        + _u(tracking, 2)
        + _u(timestamp, 6)
    )


def encode(msg_type: str, *, framed: bool = True, **fields: Any) -> bytes:
    """Encode one ITCH 5.0 message from named fields.

    With ``framed=True`` (the default) the result carries the 2-byte big-endian
    BinaryFILE length prefix, so encoded messages concatenate into a valid file.
    """
    locate = int(fields.get("stock_locate", 0))
    tracking = int(fields.get("tracking", 0))
    timestamp = int(fields.get("timestamp", 0))
    body = _header(msg_type, locate, tracking, timestamp)

    if msg_type == "S":
        body += _pad(fields["event_code"], 1)
    elif msg_type == "R":
        body += (
            _pad(fields["stock"], 8)
            + _pad(fields.get("market_category", "Q"), 1)
            + _pad(fields.get("financial_status", "N"), 1)
            + _u(int(fields.get("round_lot_size", 100)), 4)
            + _pad(fields.get("round_lots_only", "N"), 1)
            + _pad(fields.get("issue_classification", "C"), 1)
            + _pad(fields.get("issue_subtype", "  "), 2)
            + _pad(fields.get("authenticity", "P"), 1)
            + _pad(fields.get("short_sale_threshold", "N"), 1)
            + _pad(fields.get("ipo_flag", "N"), 1)
            + _pad(fields.get("luld_tier", "1"), 1)
            + _pad(fields.get("etp_flag", "N"), 1)
            + _u(int(fields.get("etp_leverage", 0)), 4)
            + _pad(fields.get("inverse_indicator", "N"), 1)
        )
    elif msg_type in ("A", "F"):
        body += (
            _u(int(fields["order_ref"]), 8)
            + _pad(fields["side"], 1)
            + _u(int(fields["shares"]), 4)
            + _pad(fields["stock"], 8)
            + _u(int(fields["price"]), 4)
        )
        if msg_type == "F":
            body += _pad(fields.get("attribution", "NSDQ"), 4)
    elif msg_type in ("E", "C"):
        body += (
            _u(int(fields["order_ref"]), 8)
            + _u(int(fields["executed_shares"]), 4)
            + _u(int(fields.get("match_number", 0)), 8)
        )
        if msg_type == "C":
            body += _pad(fields.get("printable", "Y"), 1) + _u(
                int(fields["execution_price"]), 4
            )
    elif msg_type == "X":
        body += _u(int(fields["order_ref"]), 8) + _u(
            int(fields["cancelled_shares"]), 4
        )
    elif msg_type == "D":
        body += _u(int(fields["order_ref"]), 8)
    elif msg_type == "U":
        body += (
            _u(int(fields["original_order_ref"]), 8)
            + _u(int(fields["new_order_ref"]), 8)
            + _u(int(fields["shares"]), 4)
            + _u(int(fields["price"]), 4)
        )
    elif msg_type == "P":
        body += (
            _u(int(fields["order_ref"]), 8)
            + _pad(fields["side"], 1)
            + _u(int(fields["shares"]), 4)
            + _pad(fields["stock"], 8)
            + _u(int(fields["price"]), 4)
            + _u(int(fields.get("match_number", 0)), 8)
        )
    else:
        raise ValueError(f"encoder does not support message type {msg_type!r}")

    expected = MESSAGE_LENGTHS[msg_type]
    if len(body) != expected:
        raise AssertionError(
            f"encoded {msg_type!r} is {len(body)} bytes, spec says {expected}"
        )
    return _u(len(body), 2) + body if framed else body


@dataclass(slots=True)
class _LiveOrder:
    ref: int
    side: str
    price: int
    shares: int


class _SymbolState:
    """Live order state for one symbol, with O(1) sampling and O(log n) touch.

    Every per-message operation the generator performs has to be sub-linear in the
    number of live orders, or generating a large file becomes quadratic. The first
    version of this generator was not, and it ended up dominating the very benchmark it
    exists to feed -- see FAILURES.md #7. Two structures fix it:

    * ``_refs`` plus ``_pos`` form a *random-access set*: a list for uniform sampling by
      index, and a ref -> index map so removal is a swap-with-last then a pop. Both
      O(1). The original ``random.choice(list(orders.values()))`` was O(n) per message
      and was the worst of the offenders.
    * Per-side ``price -> [refs]`` dicts, each list using the same swap-with-last trick
      via ``_lpos``, so picking an order resting *at the touch* is O(1) rather than a
      scan for a matching price.
    * Lazy-deletion heaps over the level prices give the touch in O(log n), instead of
      recomputing a max or min across every live order.
    """

    __slots__ = (
        "_ask_heap",
        "_ask_levels",
        "_bid_heap",
        "_bid_levels",
        "_lpos",
        "_pos",
        "_refs",
        "orders",
    )

    def __init__(self) -> None:
        self.orders: dict[int, _LiveOrder] = {}
        self._refs: list[int] = []
        self._pos: dict[int, int] = {}
        self._lpos: dict[int, int] = {}  # ref -> index within its level's ref list
        self._bid_levels: dict[int, list[int]] = {}
        self._ask_levels: dict[int, list[int]] = {}
        self._bid_heap: list[int] = []  # negated prices, so heapq acts as a max-heap
        self._ask_heap: list[int] = []

    def __len__(self) -> int:
        return len(self._refs)

    def add(self, order: _LiveOrder) -> None:
        self.orders[order.ref] = order
        self._pos[order.ref] = len(self._refs)
        self._refs.append(order.ref)
        levels, heap = self._side(order.side)
        bucket = levels.get(order.price)
        if bucket is None:
            bucket = levels[order.price] = []
            heapq.heappush(heap, -order.price if order.side == "B" else order.price)
        self._lpos[order.ref] = len(bucket)
        bucket.append(order.ref)

    def remove(self, ref: int) -> None:
        order = self.orders.pop(ref)
        levels, _heap = self._side(order.side)
        bucket = levels[order.price]
        index = self._lpos.pop(ref)
        last = bucket.pop()
        if last != ref:
            bucket[index] = last
            self._lpos[last] = index
        if not bucket:
            del levels[order.price]  # heap entry left behind, discarded lazily
        index = self._pos.pop(ref)
        last = self._refs.pop()
        if last != ref:  # swap-with-last keeps the sampling list dense in O(1)
            self._refs[index] = last
            self._pos[last] = index

    def sample(self, rng: random.Random) -> _LiveOrder:
        """Uniformly sample a live order in O(1)."""
        return self.orders[self._refs[rng.randrange(len(self._refs))]]

    def order_at(self, price: int, side: str) -> _LiveOrder | None:
        """A live order resting at ``price``, in O(1)."""
        levels, _heap = self._side(side)
        bucket = levels.get(price)
        return self.orders[bucket[0]] if bucket else None

    def best_bid(self) -> int | None:
        return self._touch(self._bid_heap, self._bid_levels, negated=True)

    def best_ask(self) -> int | None:
        return self._touch(self._ask_heap, self._ask_levels, negated=False)

    def _side(self, side: str) -> tuple[dict[int, list[int]], list[int]]:
        if side == "B":
            return self._bid_levels, self._bid_heap
        return self._ask_levels, self._ask_heap

    @staticmethod
    def _touch(
        heap: list[int], levels: dict[int, list[int]], *, negated: bool
    ) -> int | None:
        while heap:
            price = -heap[0] if negated else heap[0]
            if price in levels:
                return price
            heapq.heappop(heap)
        return None


@dataclass(slots=True)
class SyntheticStream:
    """Generate a coherent, spec-conformant ITCH 5.0 byte stream.

    Coherent means: every X/D/E/C/U references an order that is currently live;
    executions only ever hit the best price on their side; and adds never cross the
    opposite touch. That last constraint is what makes the synthetic stream usable as a
    *positive control* for the correctness oracle -- on this data the book-vs-exchange
    agreement rate must be 100%, so anything less is a bug in the book, not in the data.

    Reproducible: seeded RNG, so a benchmark file regenerates byte-identically.
    """

    symbols: tuple[str, ...] = ("AAPL", "MSFT", "TSLA", "NVDA")
    seed: int = 20190130
    base_price: int = 100 * PRICE_SCALE
    tick: int = 100  # $0.01 in fixed point
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def generate(self, n_messages: int) -> bytes:
        """Emit ``n_messages`` order-flow messages plus the session/directory preamble.

        Linear in ``n_messages``: every per-message operation is O(1) or O(log n).
        """
        rng = self._rng
        out: list[bytes] = []
        append = out.append
        clock = 9 * 3600 * 1_000_000_000  # 09:00:00 in nanoseconds since midnight

        append(encode("S", timestamp=clock, event_code="O"))
        for i, sym in enumerate(self.symbols, start=1):
            clock += rng.randint(1_000, 50_000)
            append(encode("R", stock_locate=i, timestamp=clock, stock=sym))
        clock += 1_000_000
        append(encode("S", timestamp=clock, event_code="Q"))

        states = {i: _SymbolState() for i in range(1, len(self.symbols) + 1)}
        next_ref = 1
        match_number = 1

        for _ in range(n_messages):
            clock += rng.randint(100, 100_000)
            locate = rng.randint(1, len(self.symbols))
            stock = self.symbols[locate - 1]
            state = states[locate]
            best_bid = state.best_bid()
            best_ask = state.best_ask()

            roll = rng.random()
            if roll < 0.45 or not len(state):
                # ADD, at a price that cannot cross the opposite touch.
                side = "B" if rng.random() < 0.5 else "S"
                if side == "B":
                    ceiling = (best_ask - self.tick) if best_ask else self.base_price
                    price = ceiling - self.tick * rng.randint(0, 5)
                else:
                    floor = (best_bid + self.tick) if best_bid else self.base_price
                    price = floor + self.tick * rng.randint(0, 5)
                price = max(price, self.tick)
                shares = rng.randint(1, 20) * 100
                ref = next_ref
                next_ref += 1
                append(
                    encode(
                        "F" if rng.random() < 0.1 else "A",
                        stock_locate=locate,
                        timestamp=clock,
                        order_ref=ref,
                        side=side,
                        shares=shares,
                        stock=stock,
                        price=price,
                    )
                )
                state.add(_LiveOrder(ref, side, price, shares))

            elif roll < 0.60:
                # EXECUTE at the touch -- this is what the oracle cross-checks.
                buy_side = rng.random() < 0.5
                touch = best_bid if buy_side else best_ask
                if touch is None:
                    continue
                order = state.order_at(touch, "B" if buy_side else "S")
                if order is None:
                    continue
                qty = min(
                    rng.randint(1, max(1, order.shares // 100)) * 100, order.shares
                )
                if rng.random() < 0.15:
                    append(
                        encode(
                            "C",
                            stock_locate=locate,
                            timestamp=clock,
                            order_ref=order.ref,
                            executed_shares=qty,
                            match_number=match_number,
                            printable="Y",
                            execution_price=order.price,
                        )
                    )
                else:
                    append(
                        encode(
                            "E",
                            stock_locate=locate,
                            timestamp=clock,
                            order_ref=order.ref,
                            executed_shares=qty,
                            match_number=match_number,
                        )
                    )
                match_number += 1
                order.shares -= qty
                if order.shares == 0:
                    state.remove(order.ref)

            elif roll < 0.75:
                # CANCEL part of an order.
                order = state.sample(rng)
                qty = min(
                    rng.randint(1, max(1, order.shares // 100)) * 100, order.shares
                )
                append(
                    encode(
                        "X",
                        stock_locate=locate,
                        timestamp=clock,
                        order_ref=order.ref,
                        cancelled_shares=qty,
                    )
                )
                order.shares -= qty
                if order.shares == 0:
                    state.remove(order.ref)

            elif roll < 0.90:
                # DELETE outright.
                order = state.sample(rng)
                append(
                    encode(
                        "D", stock_locate=locate, timestamp=clock, order_ref=order.ref
                    )
                )
                state.remove(order.ref)

            elif roll < 0.97:
                # REPLACE: delete + add at a new price, losing queue priority.
                order = state.sample(rng)
                state.remove(order.ref)  # remove first so the touch excludes it
                if order.side == "B":
                    other_ask = state.best_ask()
                    limit = (other_ask - self.tick) if other_ask else self.base_price
                    price = limit - self.tick * rng.randint(0, 3)
                else:
                    other_bid = state.best_bid()
                    limit = (other_bid + self.tick) if other_bid else self.base_price
                    price = limit + self.tick * rng.randint(0, 3)
                price = max(price, self.tick)
                shares = rng.randint(1, 20) * 100
                new_ref = next_ref
                next_ref += 1
                append(
                    encode(
                        "U",
                        stock_locate=locate,
                        timestamp=clock,
                        original_order_ref=order.ref,
                        new_order_ref=new_ref,
                        shares=shares,
                        price=price,
                    )
                )
                state.add(_LiveOrder(new_ref, order.side, price, shares))

            else:
                # TRADE (P): execution of a NON-displayable order. Must not touch the
                # displayed book -- see book.py. Emitted so the pipeline is exercised.
                ref = next_ref
                next_ref += 1
                append(
                    encode(
                        "P",
                        stock_locate=locate,
                        timestamp=clock,
                        order_ref=ref,
                        side="B" if rng.random() < 0.5 else "S",
                        shares=rng.randint(1, 10) * 100,
                        stock=stock,
                        price=best_bid or self.base_price,
                        match_number=match_number,
                    )
                )
                match_number += 1

        clock += 1_000_000
        append(encode("S", timestamp=clock, event_code="C"))
        return b"".join(out)


def write_fixture(path: str, n_messages: int, *, seed: int = 20190130) -> int:
    """Write a synthetic ITCH file and return its size in bytes."""
    data = SyntheticStream(seed=seed).generate(n_messages)
    with open(path, "wb") as handle:
        handle.write(data)
    return len(data)
