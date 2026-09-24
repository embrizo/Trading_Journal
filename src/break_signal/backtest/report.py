"""M6: did the strict defaults work, or do they only work on SOL?

Replays the engine over history (:mod:`replay`) and then asks, for every signal
it fired, what price did next — with no strategy and no discretion:

* **entry** = the confirmed close that broke the line (what the alert shows).
* **stop**  = the line itself. A break that falls back through the line it just
  cleared is the natural invalidation, and it is the level the alert already
  gives you, so nothing here is invented.
* **target** = ``entry ± rr × risk`` where ``risk = |entry - line|``.
* **outcome** = whichever the *following* candles touch first, within
  ``horizon`` bars. Still open at the horizon → closed at that bar's close.

Ambiguity is reported, never hidden: when one candle's range spans both target
and stop, the order inside the bar is unknowable, so it is counted as a loss
(the pessimistic reading) and the count is printed as ``ambiguous``.

**No metric is computed here.** Each evaluated signal becomes a synthetic closed
:class:`~break_signal.journal.models.Trade` and every number in the summary comes
from ``journal.analytics``, exactly as the live journal's do.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import aiohttp

from ..core.params import Params
from ..core.types import Candles, Signal
from ..journal import analytics
from ..journal.db import iso_to_ms
from ..journal.models import Trade
from ..data import rest as data_rest
from .replay import replay_signals

DEFAULT_RR = 2.0
DEFAULT_HORIZON = 30


def _outcome_of(candles: Candles, start: int, direction: str, entry: float,
                stop: float, target: float, horizon: int) -> tuple[str, float, int, bool]:
    """Walk forward from ``start + 1``. Returns (exit_kind, exit_price, bars, ambiguous)."""
    n = len(candles)
    last = min(start + horizon, n - 1)
    for i in range(start + 1, last + 1):
        hi, lo = float(candles.high[i]), float(candles.low[i])
        if direction == "LONG":
            hit_t, hit_s = hi >= target, lo <= stop
        else:
            hit_t, hit_s = lo <= target, hi >= stop
        if hit_t and hit_s:
            return "stop", stop, i - start, True        # unknowable order → pessimistic
        if hit_s:
            return "stop", stop, i - start, False
        if hit_t:
            return "target", target, i - start, False
    if last <= start:
        return "none", entry, 0, False                  # signal on the final bar
    return "horizon", float(candles.close[last]), last - start, False


def evaluate(candles: Candles, signals: list[Signal], rr: float = DEFAULT_RR,
             horizon: int = DEFAULT_HORIZON) -> list[dict]:
    """One row per signal: how it resolved, with R from ``analytics.r_multiple``."""
    by_ts = {int(t): i for i, t in enumerate(candles.ts)}
    rows: list[dict] = []
    for sig in signals:
        d = sig.to_dict()
        i = by_ts.get(iso_to_ms(d["time"]) if isinstance(d["time"], str) else int(d["time"]))
        if i is None or i >= len(candles) - 1:
            continue                                     # no forward data to judge it
        direction = "LONG" if d["event"] == "break_up" else "SHORT"
        entry, line = float(d["price"]), float(d["line"])
        risk = abs(entry - line)
        if risk <= 0:
            continue
        target = entry + rr * risk if direction == "LONG" else entry - rr * risk
        kind, exit_price, bars, ambiguous = _outcome_of(
            candles, i, direction, entry, line, target, horizon)
        if kind == "none":
            continue
        r = analytics.r_multiple(direction, entry, line, exit_price)
        rows.append({
            "symbol": d["symbol"], "tf": d["tf"], "time": d["time"], "event": d["event"],
            "direction": direction, "entry": entry, "line": line, "target": target,
            "exit": exit_price, "exit_kind": kind, "bars_held": bars,
            "ambiguous": ambiguous, "r_multiple": r,
            "outcome": analytics.derive_outcome(r),
            "rsi": d.get("rsi"), "atr_dist": d.get("atr_dist"), "touches": d.get("touches"),
        })
    return rows


def as_trades(rows: list[dict]) -> list[Trade]:
    """Synthetic closed trades so ``analytics`` produces every statistic."""
    return [
        Trade(id=i + 1, symbol=r["symbol"], direction=r["direction"], status="CLOSED",
              created_ts=0, updated_ts=0, tf=r["tf"], entry_price=r["entry"],
              sl_price=r["line"], exit_price=r["exit"], outcome=r["outcome"],
              r_multiple=r["r_multiple"], opened_ts=iso_to_ms(r["time"]),
              closed_ts=iso_to_ms(r["time"]))
        for i, r in enumerate(rows)
    ]


def summarize(rows: list[dict]) -> dict:
    return analytics.summarize(as_trades(rows))


def render(per_market: list[tuple[str, str, list[dict]]], rr: float, horizon: int) -> str:
    """Markdown: one line per market, then the pooled result."""
    out = [f"# Backtest hit-rate — target {rr:g}R, stop at the line, {horizon}-bar horizon", ""]
    out.append("| market | signals | W/L/BE | win | avg R | total R | PF | max DD | ambig |")
    out.append("|---|---|---|---|---|---|---|---|---|")

    def row(label: str, rows_: list[dict]) -> str:
        s = summarize(rows_)
        amb = sum(1 for r in rows_ if r["ambiguous"])
        win = "–" if s["win_rate"] is None else f"{round(s['win_rate'] * 100)}%"
        return (f"| {label} | {s['n']} | {s['wins']}/{s['losses']}/{s['be']} | {win} | "
                f"{_n(s['avg_r'])} | {_n(s['total_r'])} | {_n(s['profit_factor'])} | "
                f"{_n(s['max_drawdown_r'])} | {amb} |")

    pooled: list[dict] = []
    for symbol, tf, rows_ in per_market:
        out.append(row(f"{symbol} {tf}", rows_))
        pooled += rows_
    out.append(row("**all**", pooled))
    out.append("")
    if pooled:
        times = sorted(r["time"] for r in pooled)
        out.append(f"Signals from {times[0][:10]} to {times[-1][:10]}.")

    s = summarize(pooled)
    out.append(f"Pooled expectancy {_n(s['expectancy_r'])}R over {s['n']} signals "
               f"(avg win {_n(s['avg_win_r'])}R, avg loss {_n(s['avg_loss_r'])}R).")
    kinds: dict[str, int] = {}
    for r in pooled:
        kinds[r["exit_kind"]] = kinds.get(r["exit_kind"], 0) + 1
    out.append("Exits: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ".")
    amb = sum(1 for r in pooled if r["ambiguous"])
    if amb:
        out.append(f"{amb} signal(s) hit target and stop inside one candle and were counted "
                   f"as losses — the true result is somewhere above this line.")
    out += [
        "",
        "## How to read this",
        "",
        "- **Not a strategy result.** The exit rule above is a neutral proxy chosen to make",
        "  signals comparable, not a plan anyone trades. A different stop, target or horizon",
        "  gives different numbers; re-run with `--rr` / `--horizon` before leaning on any of it.",
        "- **No costs.** Fees, funding and slippage are not modelled. On a market whose PF is",
        "  near 1.0 they are enough to turn it negative.",
        "- **One window.** These are the last few hundred bars, i.e. one regime. A run that",
        "  looks better or worse over a shorter window usually just caught a friendlier one.",
        "- **Sample size.** Per-market counts are in the table; treat anything under ~30",
        "  signals as indicative only.",
        "- Stop = the broken line, and a signal is judged only on candles *after* it fired, so",
        "  there is no look-ahead. Every statistic comes from `journal/analytics.py`.",
    ]
    return "\n".join(out)


def _n(x, d: int = 2) -> str:
    if x is None:
        return "–"
    if isinstance(x, str):
        return x
    return f"{x:.{d}f}"


async def _main(args) -> int:
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    tfs = [t.strip() for t in args.tf.split(",") if t.strip()]
    rest = data_rest(args.exchange)
    per_market: list[tuple[str, str, list[dict]]] = []

    async with aiohttp.ClientSession() as session:
        for symbol in symbols:
            for tf in tfs:
                candles = await rest.fetch_candles(session, symbol, tf, args.limit)
                if len(candles) < args.warmup + 5:
                    print(f"{symbol} {tf}: only {len(candles)} candles, skipped", file=sys.stderr)
                    continue
                signals = replay_signals(candles, Params(), tf, symbol, args.warmup)
                rows = evaluate(candles, signals, args.rr, args.horizon)
                per_market.append((symbol, tf, rows))
                print(f"{symbol} {tf}: {len(candles)} candles, {len(signals)} signals, "
                      f"{len(rows)} judged", file=sys.stderr)

    if not per_market:
        print("nothing to report", file=sys.stderr)
        return 1

    text = render(per_market, args.rr, args.horizon)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([{"symbol": s, "tf": t, "signals": r} for s, t, r in per_market], fh, indent=1)
        print(f"wrote {args.json}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbols", default="SOL-USDT-SWAP,BTC-USDT-SWAP,ETH-USDT-SWAP")
    ap.add_argument("--tf", default="1D", help="comma-separated, e.g. 1D,4H")
    ap.add_argument("--exchange", default="okx", choices=["okx", "binance"])
    ap.add_argument("--limit", type=int, default=400, help="candles per market (~12 months on 1D)")
    ap.add_argument("--warmup", type=int, default=60)
    ap.add_argument("--rr", type=float, default=DEFAULT_RR, help="target in R")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON, help="bars to resolve a signal")
    ap.add_argument("--out", default=None, help="write markdown here instead of stdout")
    ap.add_argument("--json", default=None, help="also dump the per-signal rows")
    args = ap.parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
