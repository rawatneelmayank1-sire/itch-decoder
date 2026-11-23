"""Analysis over the landed SQLite tables, plus the committed charts.

Everything here reads from SQLite rather than from the replay in memory. That
separation is deliberate: the expensive step (decode + reconstruct) runs once and lands
its output; analysis then iterates cheaply against a queryable store. It also means the
charts in ``results/`` are reproducible from the database alone.

Prices come out of SQLite as fixed-point integers and are divided by ``PRICE_SCALE``
exactly once, at the point of plotting.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: charts are files, never windows (CI has no display)

import matplotlib.pyplot as plt
import pandas as pd

from .messages import PRICE_SCALE

NS_PER_SECOND = 1_000_000_000

# One consistent visual language across every chart.
_STYLE = {
    "figure.figsize": (10, 5.5),
    "figure.dpi": 130,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "font.size": 10,
}
_BID = "#1f5f8b"
_ASK = "#a83232"
_NEUTRAL = "#3f3f46"


def load_snapshots(conn: sqlite3.Connection, run_id: int) -> pd.DataFrame:
    """Snapshot rows joined to their tickers, with derived decimal/clock columns."""
    frame = pd.read_sql_query(
        """
        SELECT s.stock_locate, y.symbol, s.ts_ns, s.bid, s.ask, s.spread,
               s.bid_qty, s.ask_qty, s.bid_levels, s.ask_levels,
               s.depth_bid_qty, s.depth_ask_qty, s.imbalance
        FROM book_snapshots s
        LEFT JOIN symbols y
               ON y.run_id = s.run_id AND y.stock_locate = s.stock_locate
        WHERE s.run_id = ?
        ORDER BY s.stock_locate, s.ts_ns
        """,
        conn,
        params=(run_id,),
    )
    if frame.empty:
        return frame
    # Fixed point -> decimal, once, here at the presentation boundary.
    frame["spread_usd"] = frame["spread"] / PRICE_SCALE
    frame["mid_usd"] = (frame["bid"] + frame["ask"]) / 2 / PRICE_SCALE
    # ITCH timestamps are ns since midnight US/Eastern.
    frame["clock"] = pd.to_timedelta(frame["ts_ns"], unit="ns")
    frame["hour"] = frame["ts_ns"] / NS_PER_SECOND / 3600.0
    return frame


def load_message_counts(conn: sqlite3.Connection, run_id: int) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT msg_type, n FROM message_counts WHERE run_id = ? ORDER BY n DESC",
        conn,
        params=(run_id,),
    )


def load_symbol_activity(conn: sqlite3.Connection, run_id: int) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT stock_locate, symbol, messages, adds, executions,
               executed_volume, executed_notional
        FROM symbol_activity WHERE run_id = ?
        ORDER BY messages DESC
        """,
        conn,
        params=(run_id,),
    )


MESSAGE_LABELS = {
    "A": "A  Add Order",
    "F": "F  Add (MPID)",
    "D": "D  Delete",
    "X": "X  Cancel",
    "U": "U  Replace",
    "E": "E  Executed",
    "C": "C  Executed w/ Price",
    "P": "P  Trade (hidden)",
    "R": "R  Stock Directory",
    "L": "L  Mkt Participant Pos",
    "H": "H  Trading Action",
    "Y": "Y  Reg SHO",
    "S": "S  System Event",
    "Q": "Q  Cross Trade",
    "I": "I  NOII",
    "B": "B  Broken Trade",
    "N": "N  RPI",
    "K": "K  IPO Quoting",
    "J": "J  LULD Collar",
    "V": "V  MWCB Level",
    "W": "W  MWCB Status",
    "h": "h  Operational Halt",
    "O": "O  Direct Listing",
}


def chart_message_distribution(counts: pd.DataFrame, out: Path) -> Path:
    """Message-type census. Log x-axis because the distribution spans ~6 decades."""
    with plt.rc_context(_STYLE):
        fig, ax = plt.subplots(figsize=(9, 6))
        data = counts.sort_values("n")
        labels = [MESSAGE_LABELS.get(t, t) for t in data["msg_type"]]
        ax.barh(labels, data["n"], color=_NEUTRAL, height=0.7)
        ax.set_xscale("log")
        ax.set_xlabel("messages (log scale)")
        ax.set_title("ITCH 5.0 message-type distribution")
        for y, value in enumerate(data["n"]):
            ax.text(value * 1.15, y, f"{value:,}", va="center", fontsize=8)
        ax.set_xlim(right=data["n"].max() * 6)
        fig.tight_layout()
        fig.savefig(out)
        plt.close(fig)
    return out


def chart_spread_curve(frame: pd.DataFrame, out: Path) -> Path:
    """Quoted spread per symbol over the session.

    **Log y-axis, deliberately.** Across this window quoted spreads span roughly three
    orders of magnitude -- from a penny on QQQ to tens of dollars on TSLA at 05:00,
    when almost nothing is quoting pre-market. On a linear axis the single widest
    series flattens every other one onto the floor and the chart says nothing. Log
    scale is the honest way to show data with this dynamic range, not a way to hide the
    outlier: the outlier is real and is the most interesting feature of the chart.
    """
    with plt.rc_context(_STYLE):
        fig, ax = plt.subplots()
        for symbol, group in frame.groupby("symbol"):
            valid = group.dropna(subset=["spread_usd"])
            valid = valid[valid["spread_usd"] > 0]
            if valid.empty:
                continue
            # Rolling median, not mean: quoted spreads have fat tails and one moment of
            # dislocation should not drag the whole curve.
            smoothed = valid["spread_usd"].rolling(60, min_periods=5).median()
            ax.plot(valid["hour"], smoothed, label=str(symbol), linewidth=1.3)
        ax.set_yscale("log")
        ax.set_xlabel("hour of day (US/Eastern)")
        ax.set_ylabel("quoted spread, USD (log scale)")
        lo, hi = frame["hour"].min(), frame["hour"].max()
        ax.set_title(
            f"Quoted spread, {lo:.2f}h-{hi:.2f}h US/Eastern "
            "(60-sample rolling median)"
        )
        if lo < 9.5 <= hi:
            # 09:30 is the regular-session open; everything left of it is pre-market.
            ax.axvline(9.5, color="#444444", linestyle="--", linewidth=1)
            ax.annotate(
                "09:30 open",
                xy=(9.5, ax.get_ylim()[1]),
                xytext=(-6, -12),
                textcoords="offset points",
                ha="right",
                fontsize=8,
                color="#444444",
            )
        ax.legend(frameon=False, ncols=4, fontsize=9)
        fig.tight_layout()
        fig.savefig(out)
        plt.close(fig)
    return out


def chart_depth_and_imbalance(frame: pd.DataFrame, out: Path) -> Path:
    """Top-5 depth on each side and the resulting imbalance, for the busiest symbol."""
    if frame.empty:
        raise ValueError("no snapshots to chart")
    busiest = frame.groupby("symbol").size().idxmax()
    group = frame[frame["symbol"] == busiest]
    with plt.rc_context(_STYLE):
        fig, (top, bottom) = plt.subplots(
            2, 1, sharex=True, figsize=(10, 7), height_ratios=[2, 1]
        )
        top.plot(
            group["hour"],
            group["depth_bid_qty"].rolling(30, min_periods=1).median(),
            color=_BID,
            label="bid depth (top 5)",
            linewidth=1.2,
        )
        top.plot(
            group["hour"],
            group["depth_ask_qty"].rolling(30, min_periods=1).median(),
            color=_ASK,
            label="ask depth (top 5)",
            linewidth=1.2,
        )
        top.set_ylabel("shares")
        top.set_title(f"Book depth and imbalance -- {busiest}")
        top.legend(frameon=False)
        bottom.axhline(0, color="#999999", linewidth=0.8)
        bottom.plot(
            group["hour"],
            group["imbalance"].rolling(30, min_periods=1).mean(),
            color=_NEUTRAL,
            linewidth=1.1,
        )
        bottom.set_ylim(-1, 1)
        bottom.set_ylabel("imbalance")
        bottom.set_xlabel("hour of day (US/Eastern)")
        fig.tight_layout()
        fig.savefig(out)
        plt.close(fig)
    return out


def chart_top_symbols(activity: pd.DataFrame, out: Path, top_n: int = 25) -> Path:
    """Message volume is extremely concentrated -- this shows how much."""
    with plt.rc_context(_STYLE):
        fig, ax = plt.subplots(figsize=(9, 7))
        data = activity.head(top_n).iloc[::-1]
        labels = [
            s if isinstance(s, str) and s else f"locate {int(loc)}"
            for s, loc in zip(data["symbol"], data["stock_locate"], strict=True)
        ]
        ax.barh(labels, data["messages"], color=_BID, height=0.72)
        ax.set_xlabel("order-book messages")
        ax.set_title(f"Top {top_n} symbols by message volume")
        fig.tight_layout()
        fig.savefig(out)
        plt.close(fig)
    return out


def chart_spread_vs_activity(activity: pd.DataFrame, frame: pd.DataFrame, out: Path) -> Path:
    """Median spread against message count -- the liquidity/activity relationship."""
    med = (
        frame.dropna(subset=["spread_usd"])
        .groupby("stock_locate")["spread_usd"]
        .median()
        .rename("median_spread_usd")
    )
    merged = activity.merge(med, on="stock_locate", how="inner")
    merged = merged[merged["messages"] > 0]
    with plt.rc_context(_STYLE):
        fig, ax = plt.subplots()
        ax.scatter(
            merged["messages"], merged["median_spread_usd"], s=28, color=_NEUTRAL,
            alpha=0.8, edgecolors="white", linewidths=0.6,
        )
        for _, row in merged.iterrows():
            if isinstance(row["symbol"], str):
                ax.annotate(
                    row["symbol"],
                    (row["messages"], row["median_spread_usd"]),
                    textcoords="offset points", xytext=(5, 3), fontsize=8,
                )
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("order-book messages (log)")
        ax.set_ylabel("median quoted spread, USD (log)")
        ax.set_title("More message traffic, tighter spreads")
        fig.tight_layout()
        fig.savefig(out)
        plt.close(fig)
    return out


def run_analysis(db_path: str, results_dir: str, run_id: int | None = None) -> dict[str, object]:
    """Produce every chart plus a text summary. Returns a dict for the CLI to print."""
    out_dir = Path(results_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        if run_id is None:
            row = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()
            if not row or row[0] is None:
                raise ValueError(f"no runs in {db_path}; run `itch decode` first")
            run_id = int(row[0])
        snapshots = load_snapshots(conn, run_id)
        counts = load_message_counts(conn, run_id)
        activity = load_symbol_activity(conn, run_id)
        meta = conn.execute(
            "SELECT source_file, source_kind, bytes_read, messages FROM runs "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()

    charts: list[str] = []
    if not counts.empty:
        charts.append(
            str(chart_message_distribution(counts, out_dir / "message_distribution.png"))
        )
    if not snapshots.empty:
        charts.append(str(chart_spread_curve(snapshots, out_dir / "spread_curve.png")))
        charts.append(
            str(chart_depth_and_imbalance(snapshots, out_dir / "depth_imbalance.png"))
        )
    if not activity.empty:
        charts.append(str(chart_top_symbols(activity, out_dir / "top_symbols.png")))
        if not snapshots.empty:
            charts.append(
                str(
                    chart_spread_vs_activity(
                        activity, snapshots, out_dir / "spread_vs_activity.png"
                    )
                )
            )

    summary: dict[str, object] = {
        "run_id": run_id,
        "source_file": meta[0] if meta else None,
        "source_kind": meta[1] if meta else None,
        "bytes_read": meta[2] if meta else None,
        "messages": meta[3] if meta else None,
        "snapshot_rows": len(snapshots),
        "symbols_with_activity": len(activity),
        "charts": charts,
    }
    if not snapshots.empty:
        valid = snapshots.dropna(subset=["spread_usd"])
        summary["median_spread_usd"] = float(valid["spread_usd"].median())
        summary["median_imbalance"] = float(snapshots["imbalance"].median(skipna=True))
        per_symbol = (
            valid.groupby("symbol")["spread_usd"].median().sort_values().head(10)
        )
        summary["tightest_symbols"] = {k: round(v, 4) for k, v in per_symbol.items()}
    if not activity.empty:
        summary["top_symbols"] = (
            activity.head(10)[["symbol", "messages", "executed_volume"]]
            .to_dict("records")
        )
    return summary
