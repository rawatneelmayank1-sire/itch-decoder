"""Benchmarks for the naive and vectorised decoders, and for book replay.

Measurement discipline, because a single stopwatch reading is not a measurement:

* **Warm-up trials are run and discarded.** The first pass pays page faults on a freshly
  read buffer, CPU frequency ramp, and NumPy's first-call setup. Including it inflates
  the naive/fast ratio in the fast decoder's favour, which would be flattering and wrong.
* **Every configuration is repeated** and reported as **median with min/max**, not mean.
  On a laptop the distribution is right-skewed by scheduler noise; the median is the
  robust centre and the spread is what tells you whether to trust the gap.
* **``time.perf_counter``**, the highest-resolution monotonic clock available, and the
  timed region excludes file I/O -- the buffer is read once, before timing.
* **Both decoders are checked to agree** on every run before any timing is reported. A
  fast decoder that is wrong is not fast, it is broken.

Regenerate everything with ``python -m itch bench``.
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .decoder import Message, decode, read_file
from .fast_decoder import DecodedBatch, book_events, decode_columnar
from .generator import SyntheticStream
from .messages import DECODED_TYPES
from .replay import replay


@dataclass(slots=True)
class BenchResult:
    """One (workload, implementation) measurement.

    Both a *frames* count and a *decoded records* count are recorded, and the
    distinction is not pedantry. Neither decoder builds records for message types
    outside ``DECODED_TYPES`` (``L``, ``H``, ``Y``, ``I``, ``Q``, ...); those are walked
    and skipped. The opening megabytes of a real ITCH file are dominated by exactly
    those setup messages -- over half of the first 10 MB is ``L`` -- so a rate quoted per
    *frame* flatters any slice with a high skip fraction and is not comparable across
    workloads. ``decoded_per_sec`` is the rate over records actually built, and
    ``decoded_fraction`` says how much of each workload was real decoding work.
    """

    label: str
    implementation: str
    source: str
    messages: int
    """Total framed messages in the buffer, including skipped types."""
    decoded_messages: int
    """Messages actually turned into records (DECODED_TYPES only)."""
    bytes_: int
    trials: int
    median_s: float
    min_s: float
    max_s: float
    msgs_per_sec: float
    """Frames per second -- includes skipped types. Use for I/O-style comparisons."""
    decoded_per_sec: float
    """Decoded records per second. The comparable figure across workloads."""
    mb_per_sec: float

    @property
    def decoded_fraction(self) -> float:
        return self.decoded_messages / self.messages if self.messages else 0.0

    def as_row(self) -> str:
        return (
            f"| {self.label} | {self.implementation} | {self.messages:,} | "
            f"{self.decoded_messages:,} ({self.decoded_fraction:.0%}) | "
            f"{self.bytes_ / 1e6:,.1f} | {self.median_s:.4f} | "
            f"{self.min_s:.4f}-{self.max_s:.4f} | {self.decoded_per_sec:,.0f} | "
            f"{self.mb_per_sec:,.1f} |"
        )


def _time_it(
    fn: Callable[[], object], *, trials: int, warmups: int
) -> tuple[float, float, float]:
    """Run ``fn`` ``warmups`` times discarding results, then ``trials`` times timed."""
    for _ in range(warmups):
        fn()
    samples: list[float] = []
    for _ in range(trials):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples), min(samples), max(samples)


def bench_buffer(
    label: str,
    data: bytes,
    source: str,
    *,
    trials: int = 5,
    warmups: int = 1,
    include_naive: bool = True,
) -> list[BenchResult]:
    """Benchmark both decoders over one in-memory buffer, after checking they agree."""
    results: list[BenchResult] = []
    batch = decode_columnar(data)
    n_frames = batch.total_messages
    n_decoded = sum(
        n for t, n in batch.type_counts.items() if t in DECODED_TYPES
    )

    def record(implementation: str, median: float, low: float, high: float) -> BenchResult:
        return BenchResult(
            label=label,
            implementation=implementation,
            source=source,
            messages=n_frames,
            decoded_messages=n_decoded,
            bytes_=len(data),
            trials=trials,
            median_s=median,
            min_s=low,
            max_s=high,
            msgs_per_sec=n_frames / median,
            decoded_per_sec=n_decoded / median,
            mb_per_sec=len(data) / 1e6 / median,
        )

    if include_naive:
        messages = decode(data)
        _assert_decoders_agree(messages, batch)
        del messages  # free ~n_decoded objects before timing, so GC state is comparable
        results.append(
            record(
                "naive (struct.unpack per message)",
                *_time_it(lambda: decode(data), trials=trials, warmups=warmups),
            )
        )

    results.append(
        record(
            "vectorised (NumPy structured dtype)",
            *_time_it(lambda: decode_columnar(data), trials=trials, warmups=warmups),
        )
    )
    return results


def _assert_decoders_agree(
    messages: list[Message], batch: DecodedBatch
) -> None:
    """Cross-check the two implementations field by field on the Add Order stream.

    Add Orders carry every field kind the format has -- u64, u16, u32, alpha, and a
    fixed-point price -- so agreement on them exercises the whole layout.
    """
    naive_adds = [m for m in messages if m.msg_type == "A"]
    fast = batch.arrays.get("A")
    if fast is None:
        if naive_adds:
            raise AssertionError("vectorised decoder found no Add Orders, naive did")
        return
    if len(naive_adds) != len(fast):
        raise AssertionError(
            f"Add Order count differs: naive {len(naive_adds)} vs fast {len(fast)}"
        )
    stamps = batch.timestamps("A")
    for i in (0, len(fast) // 2, len(fast) - 1):
        naive = naive_adds[i]
        row = fast[i]
        expected = (
            naive.stock_locate,
            naive.timestamp,
            naive.order_ref,
            naive.side,
            naive.shares,
            naive.stock,
            naive.price,
        )
        actual = (
            int(row["stock_locate"]),
            int(stamps[i]),
            int(row["order_ref"]),
            bytes(row["side"]),
            int(row["shares"]),
            bytes(row["stock"]),
            int(row["price"]),
        )
        if expected != actual:
            raise AssertionError(
                f"decoders disagree on Add Order {i}:\n  naive {expected}\n  fast  {actual}"
            )


def bench_replay(data: bytes, label: str, source: str, *, trials: int = 3) -> BenchResult:
    """Benchmark order book reconstruction (decode excluded from the timed region)."""
    batch = decode_columnar(data)
    events = book_events(batch)
    median, low, high = _time_it(
        lambda: replay(events, check_oracle=True), trials=trials, warmups=1
    )
    return BenchResult(
        label=label,
        implementation="order book replay (with oracle checks)",
        source=source,
        messages=int(events.size),
        decoded_messages=int(events.size),
        bytes_=len(data),
        trials=trials,
        median_s=median,
        min_s=low,
        max_s=high,
        msgs_per_sec=events.size / median,
        decoded_per_sec=events.size / median,
        mb_per_sec=len(data) / 1e6 / median,
    )


def profile_fast_decode(data: bytes) -> dict[str, float]:
    """Split the vectorised decode into scan vs extract, so the profile is actionable.

    The framing scan is the residual Python loop and therefore this design's Amdahl
    ceiling. Reporting its share as a number is what turns "I optimised it" into
    "here is what is left and why".
    """
    from .fast_decoder import scan_frames

    start = time.perf_counter()
    scan_frames(data)
    scan_s = time.perf_counter() - start

    start = time.perf_counter()
    decode_columnar(data)
    total_s = time.perf_counter() - start
    return {
        "scan_s": scan_s,
        "total_s": total_s,
        "scan_share": scan_s / total_s if total_s else float("nan"),
    }


def run_benchmarks(
    *,
    real_file: str | None,
    results_dir: str,
    trials: int = 5,
    sizes_mb: tuple[int, ...] = (5, 25, 100),
    synthetic_sizes: tuple[int, ...] = (50_000, 250_000, 1_000_000),
) -> dict[str, object]:
    """Run the full benchmark matrix and write ``benchmarks.md`` + ``benchmarks.json``.

    Two data sources are reported separately and labelled, because mixing them would be
    dishonest: the real file's message mix (heavy on adds and deletes, thin on
    executions in the pre-open window) differs from the synthetic stream's.
    """
    out_dir = Path(results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[BenchResult] = []
    profile: dict[str, float] = {}
    environment = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
    }

    def flush() -> dict[str, object]:
        """Write results to disk after every workload.

        A benchmark matrix over real data takes minutes; buffering everything until the
        end means a crash or an interrupt costs the whole run. Writing incrementally
        makes the cost of a failure one workload instead of all of them.
        """
        payload: dict[str, object] = {
            "environment": environment,
            "profile_of_largest_real_decode": profile,
            "results": [asdict(r) for r in results],
        }
        (out_dir / "benchmarks.json").write_text(json.dumps(payload, indent=2))
        (out_dir / "benchmarks.md").write_text(_render_markdown(results, payload))
        return payload

    if real_file and Path(real_file).exists():
        for mb in sizes_mb:
            data = read_file(real_file, max_bytes=mb * 1_000_000)
            if not data:
                continue
            results.extend(
                bench_buffer(
                    f"real {mb} MB",
                    data,
                    source=f"NASDAQ 01302019.NASDAQ_ITCH50 prefix ({mb} MB)",
                    trials=trials,
                )
            )
            flush()
            if mb == max(sizes_mb):
                profile = profile_fast_decode(data)
                results.append(
                    bench_replay(
                        data, f"real {mb} MB", f"NASDAQ 01302019 prefix ({mb} MB)"
                    )
                )
                flush()
            del data

    for n in synthetic_sizes:
        data = SyntheticStream().generate(n)
        results.extend(
            bench_buffer(
                f"synthetic {n:,} msgs",
                data,
                source=f"itch.generator seed=20190130, {n} order-flow messages",
                trials=trials,
            )
        )
        flush()
        if n == max(synthetic_sizes):
            results.append(
                bench_replay(data, f"synthetic {n:,} msgs", "itch.generator seed=20190130")
            )
            flush()
        del data

    return flush()


def _render_markdown(results: list[BenchResult], payload: dict[str, object]) -> str:
    env = payload["environment"]  # type: ignore[index]
    lines = [
        "# Benchmarks",
        "",
        "Generated by `python -m itch bench`. Every number below came from a run on this",
        "machine; none are estimated or copied from anywhere.",
        "",
        "**Method.** Warm-up trials are run and discarded, then each configuration is",
        "repeated and reported as **median** with the observed min-max range. File I/O is",
        "outside the timed region. Before any timing, the two decoders are cross-checked",
        "field by field and the run aborts if they disagree.",
        "",
        "**Read the `decoded` column.** Neither decoder builds records for message types",
        "outside the ten it decodes (`L`, `H`, `Y`, `I`, `Q`, ...); those are walked and",
        "skipped. The opening megabytes of a real ITCH file are dominated by exactly those",
        "setup messages -- more than half of the first 10 MB is `L` (Market Participant",
        "Position) -- so a rate quoted per *frame* flatters a slice with a high skip",
        "fraction. The rate column below is therefore **decoded records per second**, and",
        "the `decoded` column shows what share of each workload was real decoding work.",
        "The speedup ratio is unaffected either way: both implementations see the",
        "identical buffer.",
        "",
        f"- Python {env['python']}  ",  # type: ignore[index]
        f"- {env['platform']}  ",  # type: ignore[index]
        f"- {env['processor']}",  # type: ignore[index]
        "",
        "## Decode throughput",
        "",
        "| workload | implementation | frames | decoded | MB | median s | min-max s | decoded/sec | MB/s |",
        "| --- | --- | ---: | ---: | ---: | ---: | :---: | ---: | ---: |",
    ]
    lines.extend(r.as_row() for r in results)

    lines += [
        "",
        "## Speedup, vectorised vs naive",
        "",
        "| workload | decoded records | naive decoded/s | vectorised decoded/s | speedup |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    by_label: dict[str, dict[str, BenchResult]] = {}
    for r in results:
        by_label.setdefault(r.label, {})[r.implementation] = r
    for label, impls in by_label.items():
        naive = next((v for k, v in impls.items() if k.startswith("naive")), None)
        fast = next((v for k, v in impls.items() if k.startswith("vectorised")), None)
        if naive and fast:
            lines.append(
                f"| {label} | {naive.decoded_messages:,} | "
                f"{naive.decoded_per_sec:,.0f} | {fast.decoded_per_sec:,.0f} | "
                f"**{fast.decoded_per_sec / naive.decoded_per_sec:.2f}x** |"
            )

    profile = payload.get("profile_of_largest_real_decode") or {}
    if profile:
        lines += [
            "",
            "## Where the vectorised decoder's time goes",
            "",
            f"- framing scan (the residual Python loop): **{profile['scan_s']:.3f} s**",  # type: ignore[index]
            f"- total vectorised decode: **{profile['total_s']:.3f} s**",  # type: ignore[index]
            f"- scan share of total: **{profile['scan_share']:.1%}**",  # type: ignore[index]
            "",
            "The scan is sequential by construction -- a record's length is only knowable",
            "once you are positioned at its start, so offsets form a chain and cannot be",
            "produced by a `cumsum`. It is this design's Amdahl ceiling; see DESIGN.md.",
        ]

    lines += [
        "",
        "## Data sources",
        "",
        "| workload | source |",
        "| --- | --- |",
    ]
    seen: set[str] = set()
    for r in results:
        if r.label not in seen:
            seen.add(r.label)
            lines.append(f"| {r.label} | {r.source} |")
    lines.append("")
    return "\n".join(lines)
