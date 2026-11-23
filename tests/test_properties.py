"""Hypothesis property-based tests.

Example-based tests check the cases you thought of. These check the cases you did not.
Hypothesis generates random *sequences* of book operations and asserts that the
structural invariants hold after every single one -- which is how you find the bug that
only appears when a partial cancel empties a level that a replace is about to target.

The invariants under test:

(a) the book never crosses (bid < ask, always);
(b) every resting quantity is strictly positive;
(c) each level's ``total_qty`` equals the sum of its orders' quantities;
(d) ``order_index`` and the level lists agree in both directions;
(e) price-time priority is preserved -- arrival order within a level is never permuted.

(c), (d) and (e) are checked by :meth:`OrderBook.check_invariants`, which walks every
list; (a) is checked separately because the operation generator is what guarantees
non-crossing.
"""

from __future__ import annotations

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from itch.book import MultiBook, OrderBook
from itch.decoder import decode
from itch.fast_decoder import book_events, decode_columnar
from itch.generator import encode
from itch.messages import BUY, SELL
from itch.replay import replay

LOCATE = 1

# ITCH field domains. Prices are fixed-point integers, hence integer strategies --
# generating float prices would not even be expressible against this API, which is
# itself a small argument for the integer design.
prices = st.integers(min_value=1, max_value=1_000_000)
quantities = st.integers(min_value=1, max_value=1_000_000)
sides = st.sampled_from([BUY, SELL])


# ------------------------------------------------------- stateful book machine


class BookMachine(RuleBasedStateMachine):
    """Drive a book through random operation sequences, checking invariants after each.

    A shadow model (``self.live``) tracks what *should* be resting. Comparing the book
    against an independently maintained plain-dict model is what makes this a real
    test rather than the book agreeing with itself.

    Adds are constrained not to cross the opposite touch, mirroring the exchange: an
    order that would cross executes instead of resting, so a non-crossing book is a
    property of correct *input*, and the test asserts we never break it ourselves.
    """

    def __init__(self) -> None:
        super().__init__()
        self.multibook = MultiBook()
        self.book: OrderBook = self.multibook.book(LOCATE)
        self.live: dict[int, tuple[bytes, int, int]] = {}  # ref -> (side, price, qty)
        self.arrival: list[int] = []  # refs in arrival order, for priority checks
        self.next_ref = 1

    # -- rules ---------------------------------------------------------------

    @rule(side=sides, price=prices, qty=quantities)
    def add(self, side: bytes, price: int, qty: int) -> None:
        # Keep the generated stream non-crossing, as a real venue's feed is.
        if side == BUY:
            ask = self.book.best_ask()
            assume(ask is None or price < ask)
        else:
            bid = self.book.best_bid()
            assume(bid is None or price > bid)
        ref = self.next_ref
        self.next_ref += 1
        self.multibook.on_add(LOCATE, ref, side, price, qty)
        self.live[ref] = (side, price, qty)
        self.arrival.append(ref)

    @precondition(lambda self: bool(self.live))
    @rule(data=st.data(), qty=quantities)
    def execute(self, data: st.DataObject, qty: int) -> None:
        ref = data.draw(st.sampled_from(sorted(self.live)))
        side, price, resting = self.live[ref]
        taken = min(qty, resting)
        self.multibook.on_execute(ref, qty)
        self._settle(ref, side, price, resting - taken)

    @precondition(lambda self: bool(self.live))
    @rule(data=st.data(), qty=quantities)
    def cancel(self, data: st.DataObject, qty: int) -> None:
        ref = data.draw(st.sampled_from(sorted(self.live)))
        side, price, resting = self.live[ref]
        taken = min(qty, resting)
        self.multibook.on_cancel(ref, qty)
        self._settle(ref, side, price, resting - taken)

    @precondition(lambda self: bool(self.live))
    @rule(data=st.data())
    def delete(self, data: st.DataObject) -> None:
        ref = data.draw(st.sampled_from(sorted(self.live)))
        self.multibook.on_delete(ref)
        del self.live[ref]
        self.arrival.remove(ref)

    @precondition(lambda self: bool(self.live))
    @rule(data=st.data(), price=prices, qty=quantities)
    def replace(self, data: st.DataObject, price: int, qty: int) -> None:
        ref = data.draw(st.sampled_from(sorted(self.live)))
        side, _old_price, _resting = self.live[ref]
        # Same non-crossing constraint as `add`: the replacement is a new order.
        if side == BUY:
            ask = min(
                (p for r, (s, p, _q) in self.live.items() if s == SELL and r != ref),
                default=None,
            )
            assume(ask is None or price < ask)
        else:
            bid = max(
                (p for r, (s, p, _q) in self.live.items() if s == BUY and r != ref),
                default=None,
            )
            assume(bid is None or price > bid)
        new_ref = self.next_ref
        self.next_ref += 1
        self.multibook.on_replace(ref, new_ref, qty, price)
        del self.live[ref]
        self.arrival.remove(ref)
        self.live[new_ref] = (side, price, qty)
        self.arrival.append(new_ref)  # back of the queue: priority is forfeited

    @rule(qty=quantities, price=prices)
    def hidden_trade(self, qty: int, price: int) -> None:
        """P messages must leave the displayed book completely untouched."""
        before = dict(self.live)
        self.multibook.on_trade(qty, price)
        assert self.live == before

    def _settle(self, ref: int, side: bytes, price: int, remaining: int) -> None:
        if remaining <= 0:
            del self.live[ref]
            self.arrival.remove(ref)
        else:
            self.live[ref] = (side, price, remaining)

    # -- invariants ----------------------------------------------------------

    @invariant()
    def structure_is_consistent(self) -> None:
        """(b), (c), (d) plus pointer integrity, and (a) via check_invariants."""
        self.book.check_invariants()

    @invariant()
    def never_crosses(self) -> None:
        """(a) explicitly, so the failure message names the right property."""
        bid, ask = self.book.best_bid(), self.book.best_ask()
        assert bid is None or ask is None or bid < ask, f"book crossed: {bid} >= {ask}"

    @invariant()
    def matches_the_shadow_model(self) -> None:
        """The book agrees with an independently maintained dict of what is resting."""
        assert set(self.book.orders) == set(self.live), (
            f"order_index {sorted(self.book.orders)} != model {sorted(self.live)}"
        )
        for ref, (side, price, qty) in self.live.items():
            node = self.book.orders[ref]
            assert node.side == side
            assert node.price == price
            assert node.shares == qty, f"ref {ref}: book {node.shares} != model {qty}"
            assert node.shares > 0  # (b)

    @invariant()
    def price_time_priority_holds(self) -> None:
        """(e): within a level, queue order must equal arrival order."""
        rank = {ref: i for i, ref in enumerate(self.arrival)}
        for levels in (self.book.bids, self.book.asks):
            for level in levels.values():
                refs = [node.ref for node in level.orders()]
                ranks = [rank[r] for r in refs]
                assert ranks == sorted(ranks), (
                    f"price-time priority violated at {level.price}: {refs}"
                )

    @invariant()
    def aggregates_match_the_model(self) -> None:
        """(c) cross-checked against the model, not just self-consistently."""
        expected: dict[tuple[bytes, int], int] = {}
        for side, price, qty in self.live.values():
            expected[(side, price)] = expected.get((side, price), 0) + qty
        for side, levels in ((BUY, self.book.bids), (SELL, self.book.asks)):
            for price, level in levels.items():
                assert level.total_qty == expected.get((side, price)), (
                    f"level {price} qty {level.total_qty} != model "
                    f"{expected.get((side, price))}"
                )


TestBookMachine = BookMachine.TestCase
TestBookMachine.settings = settings(
    max_examples=250,
    stateful_step_count=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


# ------------------------------------------------------- stateless properties


@given(
    side=sides,
    price=prices,
    qty=quantities,
    locate=st.integers(min_value=0, max_value=65535),
    ref=st.integers(min_value=0, max_value=2**64 - 1),
    timestamp=st.integers(min_value=0, max_value=2**48 - 1),
    stock=st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ.", min_size=1, max_size=8),
)
@settings(max_examples=300, deadline=None)
def test_add_order_round_trips_for_any_field_values(
    side: bytes, price: int, qty: int, locate: int, ref: int, timestamp: int, stock: str
) -> None:
    """encode -> decode is the identity across the whole field domain."""
    raw = encode(
        "A",
        stock_locate=locate,
        timestamp=timestamp,
        order_ref=ref,
        side=side.decode(),
        shares=qty,
        stock=stock,
        price=price,
    )
    (message,) = decode(raw)
    assert message.stock_locate == locate
    assert message.timestamp == timestamp
    assert message.order_ref == ref
    assert message.side == side
    assert message.shares == qty
    assert message.price == price
    assert message.stock == stock.encode().ljust(8, b" ")

    row = decode_columnar(raw).arrays["A"][0]
    assert int(row["order_ref"]) == ref
    assert int(row["price"]) == price
    assert int(row["shares"]) == qty


@given(
    refs=st.lists(
        st.integers(min_value=1, max_value=200), min_size=1, max_size=60, unique=True
    ),
    price=prices,
    qty=quantities,
)
@settings(max_examples=200, deadline=None)
def test_cancelling_in_any_order_leaves_a_consistent_level(
    refs: list[int], price: int, qty: int
) -> None:
    """Unlink order must not matter -- head, tail, middle, or all of them."""
    book = OrderBook("PROP")
    for ref in refs:
        book.add_order(ref, BUY, price, qty)
    assert book.bids[price].total_qty == qty * len(refs)
    for i, ref in enumerate(refs):
        book.remove(book.orders[ref])
        remaining = len(refs) - i - 1
        if remaining:
            assert book.bids[price].order_count == remaining
            assert book.bids[price].total_qty == qty * remaining
            book.check_invariants()
    assert price not in book.bids
    assert book.best_bid() is None
    book.check_invariants()


@given(
    quantities_list=st.lists(quantities, min_size=1, max_size=40),
    price=prices,
)
@settings(max_examples=200, deadline=None)
def test_level_total_always_equals_the_sum_of_its_orders(
    quantities_list: list[int], price: int
) -> None:
    """(c) as a standalone property, over arbitrary quantity mixes."""
    book = OrderBook("SUM")
    for i, qty in enumerate(quantities_list, start=1):
        book.add_order(i, SELL, price, qty)
    level = book.asks[price]
    assert level.total_qty == sum(quantities_list)
    assert level.total_qty == sum(n.shares for n in level.orders())
    book.check_invariants()


@given(
    bid_prices=st.lists(prices, min_size=1, max_size=25, unique=True),
    spread=st.integers(min_value=1, max_value=10_000),
)
@settings(max_examples=200, deadline=None)
def test_best_prices_are_the_extremes_of_the_live_levels(
    bid_prices: list[int], spread: int
) -> None:
    """The lazy-deletion heap must always agree with a brute-force max/min."""
    book = OrderBook("TOUCH")
    for i, price in enumerate(bid_prices, start=1):
        book.add_order(i, BUY, price, 100)
    top_bid = max(bid_prices)
    assert book.best_bid() == top_bid
    ask_prices = [top_bid + spread + i for i in range(len(bid_prices))]
    for i, price in enumerate(ask_prices, start=1000):
        book.add_order(i, SELL, price, 100)
    assert book.best_ask() == min(ask_prices)
    assert not book.is_crossed()
    # Now empty the book one order at a time; the touch must track the brute force.
    live = dict(book.orders)
    for _ref, node in live.items():
        book.remove(node)
        remaining_bids = [n.price for n in book.orders.values() if n.side == BUY]
        assert book.best_bid() == (max(remaining_bids) if remaining_bids else None)
    book.check_invariants()


@given(seed=st.integers(min_value=0, max_value=10_000), n=st.integers(50, 400))
@settings(max_examples=40, deadline=None)
def test_replaying_a_generated_stream_never_corrupts_the_book(seed: int, n: int) -> None:
    """End-to-end: generate -> decode -> replay -> assert every invariant.

    On the synthetic stream (a positive control, coherent by construction) the oracle
    must report perfect agreement. Anything less is a bug in the book, not the data.
    """
    from itch.generator import SyntheticStream

    data = SyntheticStream(seed=seed).generate(n)
    result = replay(book_events(decode_columnar(data)), check_oracle=True)
    result.multibook.check_invariants()
    oracle = result.oracle
    assert oracle.executions_checked == oracle.refs_resolved
    assert oracle.away_from_touch == 0
    assert oracle.side_empty == 0
    assert result.multibook.stats.crossed_events == 0
    assert result.multibook.stats.overfilled_executes == 0
