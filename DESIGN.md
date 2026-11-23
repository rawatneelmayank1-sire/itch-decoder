# Design

The design decisions in this project, and why each one is the way it is. Written to be
argued with.

Numbers quoted here come from `results/benchmarks.json` and `results/pipeline_summary.json`,
both regenerable with `python run_all.py`. The data source and scale are stated wherever a
figure appears.

---

## 1. Why a doubly-linked list per price level, and not a `deque`

**The operation that decides it is cancel-by-reference.**

ITCH cancels and deletes name an *order reference number*. They do not say "remove the
third order at $107.26". So the book has to go from a reference to a position in a queue
and remove it there.

- `list`: `remove()` is O(d) in the level's depth -- a linear scan plus a shift of
  everything after it.
- `collections.deque`: O(1) at the two ends only. `deque.remove()` is a linear scan.
  A deque is a doubly-linked list of *blocks*; it gives you cheap append/popleft, not
  cheap arbitrary removal, because you cannot hold a stable reference to an interior
  position.
- **Intrusive doubly-linked list**: `order_index[ref]` returns the node itself. The node
  carries its own `prev` and `next`. Unlinking is four pointer writes. **O(1),
  independent of depth.**

"Intrusive" means the list pointers are fields on the order object rather than on a
separate wrapper node. That saves an allocation and a level of indirection per order, and
crucially it makes the dict lookup return something directly unlinkable.

**Why this matters quantitatively rather than aesthetically.** On the real 2019-01-30
data used here, deletes and cancels are the *dominant* message types -- `D` + `X` together
outnumber `A`. A liquid symbol carries hundreds of orders at the touch. An O(d) cancel on
the majority of a feed's messages is the difference between a book that keeps up and one
that does not.

The linked list also gives price-time priority for free: `head` is the oldest order and
therefore the next to fill, `append` puts new orders at the `tail`. That is exactly the
exchange's queue.

**What I gave up.** Random access. There is no "the 5th order at this level" without
walking. Nothing in the workload needs it, and `total_qty` is maintained incrementally
so depth queries never walk the list either.

---

## 2. Why integer prices, and never floats

ITCH transmits prices as unsigned 32-bit fixed-point integers with **4 implied decimal
places**: `$107.26` is `1072600` on the wire.

Three independent reasons to keep them that way end to end:

1. **Correctness of the dict key.** Price levels live in `dict[price, PriceLevel]`. Binary
   floating point cannot represent `0.0001` exactly. Two economically identical prices,
   arrived at by different arithmetic paths, can produce different doubles and therefore
   hash to two different levels. The book would silently split a level in half. Integers
   make the key exact by construction.
2. **Exactness of aggregation.** Notional is `price * shares`. In integers that is exact
   and unbounded (Python ints). In doubles, summing millions of products accumulates
   rounding error, and the error is not even deterministic across summation orders.
3. **It is free.** The wire format is already an integer. Converting to float costs a
   conversion *and* throws away exactness. `price / 10_000` happens once, at the point of
   display, in `messages.price_to_decimal`.

The same reasoning carries into SQLite: prices land as `INTEGER`, not `REAL`. A test pins
that (`test_pipeline_lands_every_table` asserts the column types).

**The counter-argument, addressed.** A double has 53 bits of mantissa and could represent
every price in the u32 range exactly. True -- but only for values that *are* exactly
representable. The moment you do arithmetic (a midpoint, a spread average, a VWAP) you
leave that set, and there is no type-level guard telling you when. Integers make the
invariant structural.

---

## 3. Why heaps with lazy deletion for the touch

Best bid and best ask are needed constantly -- every spread, every snapshot, every oracle
check. Options:

- **Scan the dict**: O(n) per query in the number of live levels. Far too slow.
- **Sorted container** (e.g. a skip list or a B-tree): O(log n) insert, O(1) touch. Good,
  but a dependency and more machinery than the workload needs.
- **Heap with lazy deletion**: what this uses.

`heapq` is a min-heap, so bids are pushed negated to make it behave as a max-heap. A price
is pushed **only when its level is created**. When a level empties, it is removed from the
dict but its heap entry is *left in place*.

**Why leaving the garbage is right.** Removing an arbitrary element from a binary heap is
O(n) to find it plus O(log n) to restore the heap property. Eagerly cleaning up costs more
than the stale entries ever will.

**The amortised argument, precisely.** `best_bid()` pops until the top price is one that
still has a live level in `self.bids`. Each push costs O(log n) and each entry can be
popped at most once. Over *m* operations there are at most *m* pushes and therefore at most
*m* pops, so total heap work across the whole run is O(m log n) -- **O(log n) amortised per
operation**.

A single `best_bid()` call can be O(k log n) if it clears k stale entries. That is fine and
not adversarially exploitable: that work was already paid for by the pushes that created
those entries, and it is not repeatable -- once popped, an entry is gone. There is no input
that makes the *run* quadratic.

**The one thing to be careful about.** A price destroyed and recreated is pushed again,
so the heap can hold duplicates of a live price. That is harmless: the pruning loop stops
at the first live price, and a duplicate of a live price *is* live. Pinned by
`test_heap_survives_a_level_being_destroyed_and_recreated`, which cycles a level 20 times.

---

## 4. Why a replace loses queue priority

ITCH `U` (Order Replace) is **delete + add**, and the replacement goes to the **back** of
the queue at its new price.

This is not an implementation shortcut, it is exchange behaviour, and it is economically
load-bearing. Queue position is *earned by resting* -- it is the compensation for the risk
of having posted early and being adversely selected. If you could change your price or
increase your size while keeping your place in line, the whole incentive structure of a
price-time priority book collapses: everyone would post far from the touch to accumulate
priority, then repriced to the touch the instant they wanted to trade.

**What it would cost to get wrong.** Modelling a replace as an in-place edit that keeps
priority makes every downstream queue-position estimate optimistic. You would believe you
were third in line when you were thirtieth -- and queue position is the primary determinant
of fill probability for a passive order. A market-making backtest built on that is wrong in
the direction that flatters it.

A replace also **mints a new order reference**; the original is dead and must never resolve
again, or a later stale cancel silently hits the wrong order. `test_replace_loses_queue_priority`
and `test_replace_retires_the_original_reference` pin both halves, and the Hypothesis state
machine checks priority after every operation in a random sequence.

Note that `U` carries **no side** -- it must be inherited from the order being replaced.
That is why the replaced node's side is read before it is unlinked.

---

## 5. Endianness

ITCH is **big-endian** (network byte order). x86 is little-endian.

- `struct` handles it with the `>` prefix, which also means "standard sizes, no padding" --
  both properties are required, and the second is easy to forget.
- NumPy handles it with `>u4`-style dtype strings, plus **`align=False`**, without which
  NumPy inserts alignment padding and every field after the first misaligned one reads from
  the wrong offset.

**Why this is dangerous rather than merely fiddly.** Getting byte order wrong does not
raise. `$5.00` is `50000` = `0x0000C350`; read little-endian that is `1,347,420,160`, or
`$134,742.016`. On a chart, an obviously silly number. Buried in an aggregate, just noise.
`test_decoders_are_big_endian` asserts the literal byte pattern `\x00\x00\xc3\x50` appears
in the encoded message, so the test would fail rather than merely look odd.

**The 48-bit timestamp** deserves its own note. Timestamps are 6 bytes -- nanoseconds since
midnight US/Eastern. There is no 6-byte code in `struct` and no `uint48` in NumPy. Both
decoders split the field into a 2-byte high half and a 4-byte low half and recombine as
`hi << 32 | lo`. In the vectorised path, `ts_hi` **must** be widened to `uint64` before the
shift -- shifting a `uint16` left by 32 gives zero. `test_timestamp_uses_all_48_bits` uses
`2**48 - 1` deliberately: that is ~78 hours, well past any trading day, so a decoder that
truncated to 32 bits would still look completely plausible on real data.

---

## 6. What I optimised, and what the profile said

**The naive decoder** does one `struct.unpack_from` plus one dataclass construction per
message. That is the correctness reference and the "before" number.

**The observation that drove the optimisation:** the cost is not byte swapping, it is
CPython's per-operation overhead multiplied by tens of millions of messages.

**The optimisation** splits decoding in two:

1. **Framing scan** -- a Python loop, stripped to two byte reads, a shift, an add, and an
   `array('q').append`. No unpacking, no object construction. `array('q')` rather than a
   list so the handoff to NumPy is a zero-copy `frombuffer` instead of a per-element
   unboxing pass.
2. **Columnar extraction** -- fully vectorised. Group offsets by message type, gather each
   group's bytes into a contiguous `(n, record_length)` `uint8` block, then **reinterpret**
   it as a structured array with `.view()`. Because the dtype's itemsize equals the record
   length exactly (asserted at import), the view is a pure relabelling: no copy, no
   conversion pass. NumPy then does the big-endian conversion field-wise in C.

**Measured result** (median of 5 trials after a discarded warm-up, on the 100 MB real
slice, 3,230,304 decoded records): naive **392,151 records/sec**, vectorised **1,791,300
records/sec** -- **4.57x**. Across all six workloads the speedup is 4.1x-4.7x, except the
5 MB slice at 1.43x, which is 95% skipped setup messages and is therefore almost purely a
framing-scan benchmark.

**What the profile said, and it is the interesting part:** after vectorising the
extraction, **the framing scan dominates**. On that same slice the scan is **0.991 s of
1.777 s -- 55.7%** of the vectorised decode.

That is Amdahl's law making itself concrete. Vectorising extraction further would buy
almost nothing: even reducing extraction to zero would only take the decode from 1.777 s
to 0.991 s. The remaining Python loop is the ceiling.

**Why the scan cannot be vectorised away.** A record's length is only knowable once you are
positioned at its start, so offsets form a *chain*, not an arithmetic sequence. There is no
`cumsum` that produces them. The length prefix does not help -- reading it requires already
knowing where the record starts.

**Two things I considered and rejected**, which is the honest answer to "what would you do
next":

- *Pointer doubling.* Compute `next[p] = p + 2 + length_at(p)` for every byte position,
  vectorised, then follow the chain with log(N) doubling steps. Mathematically valid,
  O(N log N) work over N *byte positions* rather than N messages. For a 400 MB file that is
  ~27 passes over a 400-million-element int64 array. Wildly slower than the sequential loop.
- *Speculative block scan.* Split the file into blocks, resynchronise each independently
  (ITCH framing is strongly self-validating -- ~20 consecutive frames whose length prefixes
  all agree with their type bytes is essentially certain to be real alignment), then scan
  blocks in parallel. This would genuinely work and would parallelise. I did not build it
  because it trades a guarantee for a probabilistic argument, and the honest move for a
  correctness-focused project is a C extension, Cython, or Numba for the scan instead --
  same result, no soundness question.

**Other measured choices.** The gather is chunked (`_GATHER_CHUNK = 1 << 16`) so the int64
index matrix stays cache-resident rather than scaling with the file. And the replay loop
extracts columns to Python lists with `.tolist()` before iterating -- row-wise indexing into
a structured array allocates a `numpy.void` scalar per row and was the original replay
bottleneck (FAILURES.md #6).

---

## 7. The correctness oracle

**The problem.** ITCH is a delta feed. There is no state message to compare a reconstructed
book against. So how do you know it is right?

**The answer.** The exchange transmits its own Order Executed messages, and those are
statements about *the exchange's* book, not ours. Each one is a falsifiable prediction:

1. **Reference resolution.** An execution names an order reference. If we do not have it
   resting, we missed or mishandled its Add. Because this data starts at the true session
   start (system event `O` at 04:00:00), there is no warm-up excuse: every reference
   should resolve.
2. **Price-priority agreement (the headline).** Nasdaq matches with strict price priority,
   so an execution against a *displayed* buy order resting at price P proves no
   better-priced displayed bid existed at that instant. Our book must report
   `best_bid == P` immediately **before** we apply the message. This tests the price
   levels, the heaps, and the replay ordering simultaneously, against an external
   statement.
3. **Quantity feasibility.** An execution cannot remove more shares than were resting.
4. **Non-crossing.** A single venue's displayed book cannot have bid >= ask.

**Why check before applying:** the state the exchange's matching engine was looking at when
it generated the message is the state *prior* to that message.

**Why this beats a unit test.** It is the difference between "my code agrees with my other
code" and "my model agrees with reality". The synthetic generator is the positive control --
coherent by construction, so agreement there must be 100% and anything less is a bug in the
book rather than in the data. The real file is the actual test.

---

## 8. What breaks at scale

Honest limits of this design.

- **The whole file is read into memory.** Fine for the ~400 MB prefix used here; a full
  trading day is ~11 GB uncompressed. Fix: `mmap` the file (the frame scan is already a
  forward walk, so it streams naturally) and process in blocks. The columnar decoder would
  need to yield batches rather than one array per type.
- **The framing scan is single-threaded Python** and is now the dominant cost. See §6.
- **`order_index` holds every live order in a Python dict.** At peak, a full Nasdaq day has
  millions of live orders; each `OrderNode` is `__slots__`-based but still ~120 bytes plus
  the dict entry. This is the memory ceiling. A real system uses a flat array indexed by a
  dense internal id, with the order reference mapped through an open-addressing table.
- **`depth(k)` sorts the live price dict**, O(n log n). Fine for periodic snapshots, wrong
  for a per-message hot loop. If depth were needed on every message, the price levels would
  need a sorted structure rather than a plain dict.
- **Single-threaded replay.** Symbols are independent, so this shards perfectly by
  `stock_locate` -- but Python threads would not help (GIL) and processes would need the
  event array partitioned per symbol first, which the `book_events(stock_locate=...)`
  filter already supports.
- **Nasdaq only.** This is one venue's book, not the NBBO. A consolidated book needs every
  venue's feed plus clock alignment, and cross-venue "crossing" is normal, not a bug.
- **No sequence-gap handling.** The BinaryFILE format is a complete, ordered capture. A live
  MoldUDP64 multicast feed has sequence numbers, gaps, and retransmission, none of which
  this handles.

---
