"""Order book edge cases.

Each test here corresponds to something that actually goes wrong in a delta-feed
reconstruction: quantities that drift, references that outlive their orders, queue
priority silently preserved across a replace, and a book that ends up crossed.
"""

from __future__ import annotations

import pytest

from itch.book import BookError, CrossedBookError, MultiBook, OrderBook
from itch.messages import BUY, SELL

LOCATE = 1


def make_book() -> MultiBook:
    return MultiBook()


# ------------------------------------------------------------------ empty book


def test_empty_book_has_no_touch_and_no_spread() -> None:
    book = OrderBook("EMPTY")
    assert book.best_bid() is None
    assert book.best_ask() is None
    assert book.spread() is None
    assert book.mid() is None
    assert book.imbalance() is None
    assert not book.is_crossed()
    assert book.depth(5) == ([], [])
    book.check_invariants()


def test_one_sided_book_has_no_spread() -> None:
    book = OrderBook("ONESIDE")
    book.add_order(1, BUY, 100_0000, 500)
    assert book.best_bid() == 100_0000
    assert book.best_ask() is None
    assert book.spread() is None
    assert not book.is_crossed()
    book.check_invariants()


# --------------------------------------------------------------- unknown refs


def test_cancel_of_unknown_id_is_counted_not_fatal() -> None:
    """Unknown references are normal at a stream boundary; crashing would be wrong."""
    multibook = make_book()
    multibook.on_cancel(ref=999, shares=100)
    assert multibook.stats.unknown_ref_cancels == 1
    assert multibook.stats.cancels == 0
    multibook.check_invariants()


def test_delete_execute_replace_of_unknown_ids_are_counted() -> None:
    multibook = make_book()
    multibook.on_delete(ref=1)
    assert multibook.on_execute(ref=2, shares=100) is None
    multibook.on_replace(original_ref=3, new_ref=4, shares=100, price=1)
    stats = multibook.stats
    assert stats.unknown_ref_deletes == 1
    assert stats.unknown_ref_executes == 1
    assert stats.unknown_ref_replaces == 1
    assert not multibook.books or multibook.books[next(iter(multibook.books))].total_orders() == 0


def test_a_deleted_reference_is_never_resolvable_again() -> None:
    """A stale cancel arriving after a delete must not hit some other order."""
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=BUY, price=100_0000, shares=500)
    multibook.on_delete(ref=1)
    multibook.on_cancel(ref=1, shares=100)
    assert multibook.stats.unknown_ref_cancels == 1
    assert multibook.book(LOCATE).total_orders() == 0


# ------------------------------------------------------------- partial fills


def test_partial_fill_leaves_the_order_resting_with_the_remainder() -> None:
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=BUY, price=100_0000, shares=500)
    price, taken = multibook.on_execute(ref=1, shares=200)
    assert (price, taken) == (100_0000, 200)
    node = multibook.order_index[1]
    assert node.shares == 300
    level = multibook.book(LOCATE).bids[100_0000]
    assert level.total_qty == 300
    assert level.order_count == 1
    multibook.check_invariants()


def test_full_fill_removes_the_order_and_the_level() -> None:
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=BUY, price=100_0000, shares=500)
    multibook.on_execute(ref=1, shares=500)
    book = multibook.book(LOCATE)
    assert 1 not in multibook.order_index
    assert 100_0000 not in book.bids
    assert book.best_bid() is None  # stale heap entry must be skipped
    multibook.check_invariants()


def test_overfill_is_clamped_and_counted() -> None:
    """If the exchange claims more shares than we have resting, we are already wrong.

    Clamping keeps every later depth figure sane; the counter preserves the evidence.
    """
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=BUY, price=100_0000, shares=100)
    _price, taken = multibook.on_execute(ref=1, shares=500)
    assert taken == 100
    assert multibook.stats.overfilled_executes == 1
    assert multibook.book(LOCATE).total_orders() == 0
    multibook.check_invariants()


def test_partial_cancel_then_full_cancel() -> None:
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=SELL, price=101_0000, shares=1000)
    multibook.on_cancel(ref=1, shares=400)
    assert multibook.order_index[1].shares == 600
    multibook.on_cancel(ref=1, shares=600)
    assert 1 not in multibook.order_index
    assert multibook.book(LOCATE).best_ask() is None
    multibook.check_invariants()


# ------------------------------------------------------- price-time priority


def test_orders_at_one_price_keep_arrival_order() -> None:
    book = OrderBook("PRIO")
    for ref in (1, 2, 3):
        book.add_order(ref, BUY, 100_0000, 100)
    level = book.bids[100_0000]
    assert [n.ref for n in level.orders()] == [1, 2, 3]
    assert level.head.ref == 1
    assert level.tail.ref == 3


def test_cancelling_the_middle_order_is_o1_and_preserves_order() -> None:
    """The intrusive doubly-linked list exists precisely for this case.

    A deque could not remove the middle element without a linear scan.
    """
    book = OrderBook("MID")
    for ref in (1, 2, 3, 4, 5):
        book.add_order(ref, BUY, 100_0000, 100)
    book.remove(book.orders[3])
    level = book.bids[100_0000]
    assert [n.ref for n in level.orders()] == [1, 2, 4, 5]
    assert level.total_qty == 400
    assert level.order_count == 4
    book.check_invariants()


def test_removing_head_and_tail_keeps_pointers_consistent() -> None:
    book = OrderBook("ENDS")
    for ref in (1, 2, 3):
        book.add_order(ref, SELL, 100_0000, 100)
    book.remove(book.orders[1])  # head
    assert book.asks[100_0000].head.ref == 2
    book.remove(book.orders[3])  # tail
    assert book.asks[100_0000].tail.ref == 2
    assert [n.ref for n in book.asks[100_0000].orders()] == [2]
    book.check_invariants()


# -------------------------------------------------------------------- replace


def test_replace_loses_queue_priority() -> None:
    """A replaced order goes to the BACK of the queue at its new price.

    This is real exchange behaviour, not an implementation convenience: queue position
    is earned by resting, and changing price (or increasing size) forfeits it.
    """
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=BUY, price=100_0000, shares=100)
    multibook.on_add(LOCATE, ref=2, side=BUY, price=100_0000, shares=100)
    multibook.on_add(LOCATE, ref=3, side=BUY, price=100_0000, shares=100)
    # Replace the front-of-queue order, keeping the SAME price.
    multibook.on_replace(original_ref=1, new_ref=4, shares=100, price=100_0000)
    level = multibook.book(LOCATE).bids[100_0000]
    assert [n.ref for n in level.orders()] == [2, 3, 4], "replacement must go to the back"
    multibook.check_invariants()


def test_replace_retires_the_original_reference() -> None:
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=BUY, price=100_0000, shares=100)
    multibook.on_replace(original_ref=1, new_ref=2, shares=200, price=99_0000)
    assert 1 not in multibook.order_index
    assert multibook.order_index[2].shares == 200
    assert multibook.order_index[2].price == 99_0000
    multibook.on_delete(ref=1)
    assert multibook.stats.unknown_ref_deletes == 1
    multibook.check_invariants()


def test_replace_preserves_side() -> None:
    """U carries no side; it must be inherited from the order being replaced."""
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=SELL, price=101_0000, shares=100)
    multibook.on_replace(original_ref=1, new_ref=2, shares=100, price=102_0000)
    book = multibook.book(LOCATE)
    assert book.best_ask() == 102_0000
    assert book.best_bid() is None
    assert multibook.order_index[2].side == SELL


# ------------------------------------------------------------ sweeping levels


def test_sweeping_multiple_levels_walks_the_book_down() -> None:
    """A marketable order eats level by level; the touch should step each time."""
    multibook = make_book()
    for i, price in enumerate((101_0000, 102_0000, 103_0000), start=1):
        multibook.on_add(LOCATE, ref=i, side=SELL, price=price, shares=100)
    book = multibook.book(LOCATE)
    assert book.best_ask() == 101_0000
    multibook.on_execute(ref=1, shares=100)
    assert book.best_ask() == 102_0000
    multibook.on_execute(ref=2, shares=100)
    assert book.best_ask() == 103_0000
    multibook.on_execute(ref=3, shares=100)
    assert book.best_ask() is None
    assert multibook.stats.executed_volume == 300
    multibook.check_invariants()


def test_depth_ladder_is_sorted_best_first() -> None:
    book = OrderBook("LADDER")
    for price in (99_0000, 100_0000, 98_0000):
        book.add_order(price, BUY, price, 100)
    for price in (102_0000, 101_0000, 103_0000):
        book.add_order(price, SELL, price, 200)
    bids, asks = book.depth(3)
    assert [p for p, _q in bids] == [100_0000, 99_0000, 98_0000]
    assert [p for p, _q in asks] == [101_0000, 102_0000, 103_0000]
    assert all(q == 100 for _p, q in bids)
    assert all(q == 200 for _p, q in asks)


def test_level_total_qty_aggregates_all_orders_at_that_price() -> None:
    book = OrderBook("AGG")
    book.add_order(1, BUY, 100_0000, 100)
    book.add_order(2, BUY, 100_0000, 250)
    book.add_order(3, BUY, 100_0000, 75)
    assert book.bids[100_0000].total_qty == 425
    assert book.bids[100_0000].order_count == 3
    book.check_invariants()


# ------------------------------------------------------------- crossed books


def test_crossed_book_is_detected() -> None:
    book = OrderBook("CROSS")
    book.add_order(1, BUY, 101_0000, 100)
    book.add_order(2, SELL, 100_0000, 100)  # ask below bid: impossible on one venue
    assert book.is_crossed()
    with pytest.raises(CrossedBookError):
        book.check_invariants()


def test_locked_book_counts_as_crossed() -> None:
    """bid == ask is 'locked'. On a single venue's displayed book it cannot persist."""
    book = OrderBook("LOCK")
    book.add_order(1, BUY, 100_0000, 100)
    book.add_order(2, SELL, 100_0000, 100)
    assert book.is_crossed()


def test_strict_mode_rejects_a_crossing_add() -> None:
    multibook = MultiBook(strict=True)
    multibook.on_add(LOCATE, ref=1, side=BUY, price=101_0000, shares=100)
    with pytest.raises(CrossedBookError):
        multibook.on_add(LOCATE, ref=2, side=SELL, price=100_0000, shares=100)


def test_non_strict_mode_counts_crossings_and_continues() -> None:
    multibook = MultiBook(strict=False)
    multibook.on_add(LOCATE, ref=1, side=BUY, price=101_0000, shares=100)
    multibook.on_add(LOCATE, ref=2, side=SELL, price=100_0000, shares=100)
    assert multibook.stats.crossed_events == 1
    assert multibook.book(LOCATE).is_crossed()


# ------------------------------------------------------ hidden-liquidity trades


def test_trade_message_does_not_touch_the_book() -> None:
    """P reports an execution of a NON-displayable order.

    Applying it to the book would delete displayed liquidity that is genuinely still
    there -- the book would slowly drain and spreads would widen for no reason.
    """
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=BUY, price=100_0000, shares=500)
    before = multibook.book(LOCATE).total_orders()
    multibook.on_trade(shares=300, price=100_5000)
    assert multibook.book(LOCATE).total_orders() == before
    assert multibook.order_index[1].shares == 500
    assert multibook.stats.trades_non_displayed == 1
    assert multibook.stats.executed_volume == 300  # counted as volume, though
    multibook.check_invariants()


# ------------------------------------------------------------ non-printable C


def test_non_printable_execution_consumes_liquidity_but_not_volume() -> None:
    """A non-printable C still removes shares; it just must not reach the tape."""
    multibook = make_book()
    multibook.on_add(LOCATE, ref=1, side=BUY, price=100_0000, shares=500)
    multibook.on_execute(ref=1, shares=200, price=100_2500, printable=False)
    assert multibook.order_index[1].shares == 300
    assert multibook.stats.executed_volume == 0
    assert multibook.stats.executes_with_price == 1


# ------------------------------------------------------------------ multibook


def test_execute_routes_to_the_right_symbol_without_a_ticker() -> None:
    """E/C/X/D carry ONLY an order reference -- no ticker, no side, no price.

    The shared order index is the only thing that makes routing possible.
    """
    multibook = make_book()
    multibook.on_add(1, ref=100, side=BUY, price=50_0000, shares=100)
    multibook.on_add(2, ref=200, side=BUY, price=70_0000, shares=100)
    multibook.on_execute(ref=200, shares=100)
    assert multibook.book(1).total_orders() == 1
    assert multibook.book(2).total_orders() == 0
    multibook.check_invariants()


def test_heap_survives_a_level_being_destroyed_and_recreated() -> None:
    """Lazy deletion leaves duplicate prices in the heap; that must stay correct."""
    book = OrderBook("RECREATE")
    for _ in range(20):
        book.add_order(1, BUY, 100_0000, 100)
        assert book.best_bid() == 100_0000
        book.remove(book.orders[1])
        assert book.best_bid() is None
    book.add_order(2, BUY, 99_0000, 100)
    assert book.best_bid() == 99_0000
    book.check_invariants()


def test_invariant_checker_actually_catches_corruption() -> None:
    """A test suite whose assertion function cannot fail is not a test suite."""
    book = OrderBook("CORRUPT")
    book.add_order(1, BUY, 100_0000, 100)
    book.bids[100_0000].total_qty = 999  # deliberately desynchronise the aggregate
    with pytest.raises(BookError, match="total_qty"):
        book.check_invariants()


def test_invariant_checker_catches_a_stale_empty_level() -> None:
    book = OrderBook("STALE")
    book.add_order(1, BUY, 100_0000, 100)
    level = book.bids[100_0000]
    level.unlink(book.orders[1])  # unlink without dropping the level
    with pytest.raises(BookError, match="empty"):
        book.check_invariants()
