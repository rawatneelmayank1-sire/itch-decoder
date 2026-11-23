"""End-to-end pipeline tests: generator -> decode -> replay -> SQLite -> charts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from itch.analyse import run_analysis
from itch.cli import main
from itch.generator import write_fixture
from itch.pipeline import run_pipeline


@pytest.fixture(scope="module")
def synthetic_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("data") / "synthetic.itch"
    write_fixture(str(path), 30_000)
    return path


def test_pipeline_lands_every_table(synthetic_file: Path, tmp_path: Path) -> None:
    db = tmp_path / "itch.db"
    result = run_pipeline(
        str(synthetic_file),
        db_path=str(db),
        symbols=("AAPL", "MSFT", "TSLA", "NVDA"),
        snapshot_interval_ns=10_000_000,
        source_kind="synthetic",
        verbose=False,
    )
    assert result.messages > 0
    assert result.book_events > 0
    assert result.snapshots > 0
    # Positive control: the synthetic stream is coherent by construction.
    assert result.oracle["ref_resolution_rate"] == 1.0
    assert result.oracle["agreement_rate"] == 1.0
    assert result.oracle["crossed_snapshots"] == 0

    conn = sqlite3.connect(db)
    try:
        for table in (
            "runs", "symbols", "book_snapshots", "message_counts",
            "symbol_activity", "oracle_report",
        ):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n > 0, f"{table} is empty"
        # Prices must land as INTEGERs, never REAL.
        types = {
            row[1]: row[2]
            for row in conn.execute("PRAGMA table_info(book_snapshots)")
        }
        assert types["bid"] == "INTEGER"
        assert types["ask"] == "INTEGER"
        assert types["spread"] == "INTEGER"
        assert types["ts_ns"] == "INTEGER"
    finally:
        conn.close()


def test_pipeline_is_rerunnable_and_appends_a_new_run(
    synthetic_file: Path, tmp_path: Path
) -> None:
    """Re-running must append a run, not corrupt the previous one."""
    db = tmp_path / "rerun.db"
    kwargs = {
        "db_path": str(db), "symbols": ("AAPL",),
        "snapshot_interval_ns": 50_000_000,
        "source_kind": "synthetic", "verbose": False,
    }
    first = run_pipeline(str(synthetic_file), **kwargs)  # type: ignore[arg-type]
    second = run_pipeline(str(synthetic_file), **kwargs)  # type: ignore[arg-type]
    assert second.run_id == first.run_id + 1
    conn = sqlite3.connect(db)
    try:
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert runs == 2
        for run_id in (first.run_id, second.run_id):
            n = conn.execute(
                "SELECT COUNT(*) FROM book_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            assert n > 0
    finally:
        conn.close()


def test_analysis_writes_charts(synthetic_file: Path, tmp_path: Path) -> None:
    db = tmp_path / "charts.db"
    results = tmp_path / "results"
    run_pipeline(
        str(synthetic_file), db_path=str(db), symbols=("AAPL", "MSFT", "TSLA"),
        snapshot_interval_ns=10_000_000, source_kind="synthetic", verbose=False,
    )
    summary = run_analysis(str(db), str(results))
    assert summary["snapshot_rows"] > 0
    assert summary["charts"], "no charts produced"
    for chart in summary["charts"]:  # type: ignore[union-attr]
        assert Path(chart).exists()
        assert Path(chart).stat().st_size > 1000  # a real PNG, not a stub


def test_cli_generate_decode_book_analyse(tmp_path: Path) -> None:
    """Exercise the documented commands end to end through argv."""
    itch_file = tmp_path / "cli.itch"
    db = tmp_path / "cli.db"
    results = tmp_path / "cli_results"

    assert main(["generate", str(itch_file), "--messages", "8000"]) == 0
    assert itch_file.exists()

    assert main([
        "decode", str(itch_file), "--db", str(db),
        "--symbols", "AAPL", "MSFT", "--snapshot-seconds", "0.01",
        "--kind", "synthetic",
    ]) == 0

    assert main(["book", "AAPL", "--file", str(itch_file), "--levels", "5"]) == 0
    assert main(["analyse", "--db", str(db), "--results", str(results)]) == 0
    assert (results / "message_distribution.png").exists()


def test_cli_reports_a_missing_file_cleanly(tmp_path: Path) -> None:
    assert main(["decode", str(tmp_path / "nope.itch")]) == 2


def test_cli_reports_an_unknown_symbol_cleanly(synthetic_file: Path) -> None:
    assert main(["book", "ZZZZ", "--file", str(synthetic_file)]) == 2


def test_benchmarks_are_regenerable(synthetic_file: Path, tmp_path: Path) -> None:
    """`itch bench` must produce both artefacts and cross-check the two decoders."""
    from itch.bench import run_benchmarks

    payload = run_benchmarks(
        real_file=None,
        results_dir=str(tmp_path),
        trials=2,
        synthetic_sizes=(2_000,),
    )
    assert payload["results"]
    assert (tmp_path / "benchmarks.md").exists()
    assert (tmp_path / "benchmarks.json").exists()
    text = (tmp_path / "benchmarks.md").read_text()
    assert "median" in text
    assert "Data sources" in text
