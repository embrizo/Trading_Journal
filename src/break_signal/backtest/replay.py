"""Replay the engine bar-by-bar over history and emit a CSV of every signal.

Usage:
    python -m break_signal.backtest.replay --symbol SOL-USDT-SWAP --tf 1D \
        --limit 500 --out signals.csv

This walks a growing window so each bar sees only the data available at the
time — the same information the live watcher has — which faithfully reproduces
the pivot-confirmation lag (no look-ahead / repainting).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys

import aiohttp

from ..config import bar_seconds
from ..core.engine import Engine
from ..core.params import Params
from ..core.retest import PendingRetest
from ..core.types import Candles, Signal
from ..data import rest as data_rest


def replay_signals(candles: Candles, params: Params, tf: str, symbol: str,
                   warmup: int = 60) -> list[Signal]:
    """Walk a growing window and return every signal in the order it fired.

    When ``params.use_retest`` is set the walk also reconstructs pending retests
    exactly as the live watcher does — a pending armed on each break, fed back to
    ``evaluate``, retired on the engine's ``retire`` list — so the retest's
    second signal appears right after its break. Off by default: identical to the
    old break-only replay.
    """
    engine = Engine(params, bar_seconds(tf), symbol, "OKX", tf)
    broken: set[str] = set()
    pending: dict[str, PendingRetest] = {}
    out: list[Signal] = []
    n = len(candles)
    for end in range(warmup, n + 1):
        window = Candles(
            ts=candles.ts[:end], open=candles.open[:end], high=candles.high[:end],
            low=candles.low[:end], close=candles.close[:end], volume=candles.volume[:end],
        )
        res = engine.evaluate(window, broken, list(pending.values()))
        last_ts = int(window.ts[end - 1])
        for sig in res.signals:
            is_retest = sig.event.startswith("retest")
            if not is_retest and sig.line_id in broken:
                continue
            out.append(sig)
            if is_retest:
                continue
            broken.add(sig.line_id)
            if params.use_retest and sig._line is not None:
                pending[sig.line_id] = PendingRetest(
                    line_id=sig.line_id, side=sig._line.side, ts_ref=last_ts,
                    value_ref=sig.line, slope=sig._line.slope,
                    touches=sig.touches, broke_ts=last_ts,
                )
        for line_id, _reason in res.retire:
            pending.pop(line_id, None)
    return out


def replay(candles: Candles, params: Params, tf: str, symbol: str, warmup: int = 60):
    """CSV-shaped rows (kept for the existing CLI/CSV path)."""
    return [s.to_dict() for s in replay_signals(candles, params, tf, symbol, warmup)]


def write_to_journal(db_path: str, signals: list[Signal], source: str = "backtest") -> tuple[int, int]:
    """Insert replay signals into the journal so the AI has history from day 1.
    Returns ``(inserted, total)``; re-runs are idempotent."""
    from ..journal.db import JournalDB, iso_to_ms

    db = JournalDB(db_path)
    try:
        before = db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        for sig in signals:
            db.insert_signal(sig, source=source, candle_ts=iso_to_ms(sig.time))
        after = db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    finally:
        db.close()
    return after - before, len(signals)


async def _main(args) -> int:
    async with aiohttp.ClientSession() as session:
        candles = await data_rest(args.exchange).fetch_candles(
            session, args.symbol, args.tf, args.limit)
    if len(candles) == 0:
        print("No candles fetched.", file=sys.stderr)
        return 1
    signals = replay_signals(candles, Params(), args.tf, args.symbol)
    if not signals:
        print(f"No signals over {len(candles)} candles of {args.symbol} {args.tf}.")
        return 0
    rows = [s.to_dict() for s in signals]
    fields = list(rows[0].keys())
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    ups = sum(1 for r in rows if r["event"] == "break_up")
    dns = len(rows) - ups
    print(
        f"{len(rows)} signals over {len(candles)} candles "
        f"({ups} up / {dns} down) -> {args.out}"
    )
    if args.to_journal:
        inserted, total = write_to_journal(args.to_journal, signals)
        print(f"journal: {inserted} new of {total} signals -> {args.to_journal}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest the break-signal rules on OKX history")
    ap.add_argument("--symbol", default="SOL-USDT-SWAP")
    ap.add_argument("--exchange", default="okx", choices=["okx", "binance"],
                    help="where the candles come from (the symbol stays an instId)")
    ap.add_argument("--tf", default="1D")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--out", default="signals.csv")
    ap.add_argument("--to-journal", metavar="DB", nargs="?", const="data/journal.db", default=None,
                    help="also store the signals in the journal (source=backtest); "
                         "default path data/journal.db")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
