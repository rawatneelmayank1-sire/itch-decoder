"""Regenerate every committed result: full-scale real-data run, charts, benchmarks.

Sequential by design -- benchmarks must not share the CPU with the pipeline or the
timings are meaningless. Resumable: each stage checks for its output and skips if
present, so a crash costs one stage rather than the run. Delete results/ to force.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

HERE = Path(__file__).parent
REAL = HERE / "data" / "01302019.NASDAQ_ITCH50.prefix.itch"
DB = HERE / "itch.db"
RESULTS = HERE / "results"


def stage(name: str, marker: Path, fn) -> None:
    if marker.exists():
        print(f"[skip] {name} -- {marker.name} already on disk", flush=True)
        return
    print(f"[run ] {name}", flush=True)
    t0 = time.time()
    fn()
    print(f"[done] {name} in {time.time() - t0:.1f}s", flush=True)


def do_pipeline() -> None:
    from itch.pipeline import run_pipeline

    result = run_pipeline(str(REAL), db_path=str(DB), source_kind="real")
    (RESULTS / "pipeline_summary.json").write_text(
        json.dumps(
            {
                "run_id": result.run_id,
                "source_file": REAL.name,
                "source_kind": "real NASDAQ TotalView-ITCH 5.0, 2019-01-30",
                "bytes_read": result.bytes_read,
                "messages": result.messages,
                "book_events": result.book_events,
                "snapshots": result.snapshots,
                "decode_seconds": result.decode_seconds,
                "replay_seconds": result.replay_seconds,
                "decode_msgs_per_sec": result.messages / result.decode_seconds,
                "replay_msgs_per_sec": result.book_events / result.replay_seconds,
                "oracle": result.oracle,
                "type_counts": result.type_counts,
                "symbols_tracked": result.symbols_tracked,
            },
            indent=2,
        )
    )


def do_analysis() -> None:
    from itch.analyse import run_analysis

    summary = run_analysis(str(DB), str(RESULTS))
    (RESULTS / "analysis_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )


def do_bench() -> None:
    from itch.bench import run_benchmarks

    run_benchmarks(
        real_file=str(REAL) if REAL.exists() else None,
        results_dir=str(RESULTS),
        trials=5,
    )


if __name__ == "__main__":
    RESULTS.mkdir(exist_ok=True)
    stage("full-scale pipeline on real ITCH data", RESULTS / "pipeline_summary.json", do_pipeline)
    stage("pandas analysis + charts", RESULTS / "analysis_summary.json", do_analysis)
    stage("benchmark matrix", RESULTS / "benchmarks.json", do_bench)
    print("ALL STAGES COMPLETE", flush=True)
