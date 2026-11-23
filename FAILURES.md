# Failures

Bugs I actually hit while building this, written down as they happened. Symptoms,
causes, and what the numbers were. Binary protocol work produces a specific and
unglamorous class of bug: the code does not crash, it produces plausible wrong numbers.
Most of what follows is about making those bugs loud instead of quiet.

---

## 1. A corrupt length prefix at the end of a buffer was silently swallowed

**Symptom.** A test that deliberately corrupted a message's length prefix -- claiming 20
bytes for an Order Delete, which the spec fixes at 19 -- expected `ITCHFormatError` and
got nothing at all. `decode()` returned an empty list and reported success.

**Cause.** Ordering. The frame walker did this:

```python
payload = pos + 2
if payload + length > end:
    return                      # "truncated tail"
type_byte = view[payload]       # desync check happened here, too late
```

The buffer was 21 bytes (2-byte prefix + 19-byte payload). With the corrupted length of
20, `2 + 20 = 22 > 21`, so the fit check fired first and the frame was classified as an
honest truncated tail. **A corrupt length makes a frame overrun the buffer, which is
byte-for-byte indistinguishable from a message that was genuinely cut short.** Every
real file this project reads is a byte-range prefix that ends mid-message, so the
truncated-tail path is exercised constantly -- it was the last place I would have looked
for a validation hole.

**Fix.** Validate the type/length agreement *before* the fit check. The type byte is
readable whenever `payload < end`, regardless of whether the rest of the record is, so
there is no reason to defer to it. Ordering is now load-bearing and commented as such in
`decoder.iter_frames`.

The vectorised path had the same hole in a different shape. Its hot loop deliberately
does no validation (that is the whole point of the optimisation), so it simply stopped
at the first frame that did not fit. The fix there is `fast_decoder._validate_tail`: the
loop stays lean, and the leftover bytes after the last complete frame get validated once,
afterwards. Same guarantee, no cost in the hot loop.

**Related, found while fixing it.** `scan_frames` was skipping validation of the *last*
complete frame with `actual[-1] = expected[-1]` -- literally asserting a value against
itself, so the final message's length was never checked. Its span is in fact observable:
the scan already tracks how many bytes it consumed, so `actual[-1] = pos -
payload_offsets[-1]`. A self-comparing assertion is worse than no assertion, because it
reads like coverage.

---

## 2. The layout assertions were the highest-value 20 lines in the project

**Not a bug that shipped, but the reason several never did.** `messages._validate_layouts`
runs at import time and asserts every NumPy dtype's `itemsize` and every `struct` format's
`calcsize` against the published spec length.

Two things it catches that are otherwise invisible:

- **NumPy inserts alignment padding by default.** `np.dtype([...])` with narrow fields
  followed by wide ones is not packed. ITCH is packed with no padding. Measured on the
  actual layouts in this project (`align=True` vs `align=False`): Add Order becomes **48**
  bytes instead of 36, Order Delete **24** instead of 19, Trade **56** instead of 44 --
  *every one of the ten decoded types differs*. Every field after the first padded one
  would then read from the wrong offset. No exception, just wrong prices.
- **Field-table transcription errors.** Writing out 23 message layouts by hand from the
  spec, a wrong width is a certainty rather than a risk. The assertion turns it into an
  `ImportError` on the first run instead of a plausible number in a chart three hours
  later.

The general lesson, and the one I would give as the reason this project has an oracle at
all: in binary parsing, *the failure mode is not a crash, it is a plausible wrong
number*. Every design decision here is about converting silent wrongness into loud
wrongness.

---

## 3. Non-displayable trades (`P`) must not touch the book

**What would have gone wrong.** ITCH `P` messages report an execution against
*non-displayable* liquidity -- hidden orders that were never in the displayed book. The
message carries an order reference number, a side, a size and a price, and looks exactly
like something you should apply to the book.

If you do, the book slowly drains: you delete displayed liquidity that is genuinely still
resting. Spreads widen for no reason and depth decays over the session, and because it is
gradual it looks like a plausible market regime rather than a bug.

I caught this from the spec rather than from a failing test, but it is worth recording
because it is the single most likely place to get book reconstruction wrong, and because
the counter-check is cheap: `MultiBook.on_trade` deliberately touches only the volume
statistics, and `test_trade_message_does_not_touch_the_book` pins that behaviour. On the
real 2019-01-30 data this path fires on hundreds of messages.

---

## 4. Choosing what the oracle should actually assert

**First attempt, too weak.** Check that every execution's order reference resolves to
something in our index. That catches missed adds, and it does confirm a real property --
but it is satisfied by any book that merely *remembers* orders. A book with completely
broken price levels would score 100%.

**Second attempt, too strong.** Check that the execution price equals the mid. Wrong on
its face: executions happen at the touch, not the mid.

**What it settled on.** Nasdaq matches with strict price priority. So an execution
against a *displayed* order resting at price P proves that no better-priced displayed
order existed on that side at that instant. Our book must therefore report `best_bid ==
P` (for a resting buy) immediately *before* the message is applied. That is a genuinely
falsifiable prediction about the price levels, the heaps, and the ordering of the replay
all at once -- and it is checked against a statement the exchange made, not against
another part of my own code.

The subtlety that makes it work: the check must run *before* applying the message, against
the state the exchange's matching engine was itself looking at.

---

## 5. Deciding what to do when the exchange contradicts us

An `E` message can name more shares than we have resting on that order. If our book is
right this cannot happen, so it is evidence of a bug -- but the question is what the code
should *do*.

Letting the quantity go negative corrupts every subsequent depth figure and total, and
turns one localised error into a permanently wrong book. Raising kills the run over a
single message. `OrderBook.reduce` clamps to what is actually resting and
`BookStats.overfilled_executes` counts the event, so the run continues on sane state and
the evidence is preserved and reported. Same reasoning for cancels and deletes of unknown
references, which are counted rather than fatal.

On the real data used here, all of these counters read **zero** -- which is the outcome
that makes the number worth reporting. A counter that is always zero on correct input is
a real check; a counter with no denominator is decoration.

---

## 6. Row-wise access into a NumPy structured array is a trap

**Symptom.** The first version of the replay loop indexed the event array row by row
(`events[i]["order_ref"]`). Replay was far slower than the decode it was consuming, which
made no sense for what is a handful of dict operations per message.

**Cause.** Indexing a structured array by row constructs a `numpy.void` scalar record
object per access, then each field access goes through it. The per-row object allocation
dominated everything the order book was actually doing.

**Fix.** Pull each column out to a Python list once (`events["order_ref"].tolist()`) and
zip the loop over plain Python ints. `tolist()` converts to native Python objects in one
C pass, and the loop body then does no NumPy work at all. This is the shape of
`replay.replay` today, and the reason for the comment there.

The general point: NumPy is fast when you stay columnar. The moment you go row-wise you
pay more than a plain Python list would have cost.

---

## 7. The synthetic data generator was accidentally quadratic, and it hijacked the benchmark

**Symptom.** The benchmark matrix stalled. The real-data workloads (5 / 25 / 100 MB) all
finished, then the run sat on `synthetic 1,000,000 msgs` for over ten minutes. That made
no sense: 1M synthetic messages is a 33 MB buffer, and the decoder had just chewed through
100 MB of real data in seconds.

**Measuring instead of guessing.** Timing the *generator* alone, doubling the size each
time:

| messages | time | rate |
| ---: | ---: | ---: |
| 20,000 | 2.64 s | 7,571 msg/s |
| 40,000 | 9.14 s | 4,376 msg/s |
| 80,000 | 42.45 s | 1,884 msg/s |

Doubling the input multiplied the time by ~4.6 and *halved* the throughput. That is the
signature of a quadratic algorithm, and it was in the test fixture, not the code under
test. Extrapolated, 1M messages would have taken around two hours.

**Cause.** Two O(live orders) operations, executed once per generated message:

```python
bids = [o for o in book.values() if o.side == "B"]   # rebuild both sides, every message
asks = [o for o in book.values() if o.side == "S"]
order = rng.choice(list(book.values()))              # materialise every order, to pick one
```

Adds outnumber removals in the generated mix, so the live book grows without bound; each
message therefore scanned an ever-larger dict. `random.choice(list(...))` was the worse of
the two, because it built a fresh list of every live order just to pick one element.

**Fix.** `_SymbolState` now maintains three structures, all O(1) or O(log n) per message:

- a *random-access set* (`_refs` list + `_pos` ref->index map) so uniform sampling is one
  index and removal is a swap-with-last;
- per-side `price -> [refs]` dicts, each using the same swap-with-last trick, so grabbing
  an order resting *at the touch* is O(1) instead of a scan for a matching price;
- lazy-deletion heaps over the level prices for best bid/ask.

**Result.** Linear, and the constant is far better:

| messages | time | rate | time ratio vs previous |
| ---: | ---: | ---: | ---: |
| 50,000 | 0.37 s | 133,891 msg/s | -- |
| 100,000 | 0.77 s | 130,429 msg/s | 2.05x |
| 200,000 | 1.58 s | 126,812 msg/s | 2.06x |
| 400,000 | 3.27 s | 122,477 msg/s | 2.07x |
| 1,000,000 | 8.42 s | 118,739 msg/s | 2.58x |

Doubling the input now doubles the time. At 80,000 messages the rewrite is roughly **68x**
faster (42.45 s to ~0.6 s), and because the old version was quadratic the gap widens with
size. The test suite also dropped from 29.4 s to 19.1 s, since almost every test builds a
synthetic stream.

**The lesson, which is the reason this one is worth writing down.** I was about to publish
a decoder benchmark whose runtime was dominated by the *fixture generator*, and I would
have reported it as decoder throughput. The tell was the shape of the scaling curve, not
the absolute time — an implementation being "slow" tells you nothing, but throughput
falling as input grows tells you exactly what class of bug you have. Test infrastructure
needs its complexity thought about too, especially when it feeds a benchmark.
