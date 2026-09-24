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
from datetime import datetime, timezone

import aiohttp

from ..config import bar_seconds, load_config
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
        hi, lo, op = float(candles.high[i]), float(candles.low[i]), float(candles.open[i])
        if direction == "LONG":
            hit_t, hit_s = hi >= target, lo <= stop
            # A stop is a stop-market order: a bar that opens through it fills at
            # the open, not at the stop, so a gap costs more than 1R. A target is a
            # limit order: it fills at the limit and a favourable gap is not
            # credited. Pricing both exactly would make every loss exactly -1R.
            fill_s = min(op, stop)
        else:
            hit_t, hit_s = lo <= target, hi >= stop
            fill_s = max(op, stop)
        if hit_t and hit_s:
            return "stop", fill_s, i - start, True      # unknowable order → pessimistic
        if hit_s:
            return "stop", fill_s, i - start, False
        if hit_t:
            return "target", target, i - start, False
    if last <= start:
        return "none", entry, 0, False                  # signal on the final bar
    if last - start < horizon:
        # Ran out of data before the horizon was up. It neither hit nor failed, so
        # counting its interim close as an outcome would be right-censoring: those
        # are near-0R by construction and would drag expectancy toward zero.
        return "truncated", float(candles.close[last]), last - start, False
    return "horizon", float(candles.close[last]), last - start, False


def evaluate(candles: Candles, signals: list[Signal], rr: float = DEFAULT_RR,
             horizon: int = DEFAULT_HORIZON) -> tuple[list[dict], int]:
    """``(rows, unresolved)`` — one row per *judged* signal, with R from
    ``analytics.r_multiple``, plus a count of the signals at the end of the series
    that had too little forward data to judge. Those are excluded rather than
    counted as flat outcomes."""
    by_ts = {int(t): i for i, t in enumerate(candles.ts)}
    rows: list[dict] = []
    unresolved = 0
    for sig in signals:
        d = sig.to_dict()
        i = by_ts.get(iso_to_ms(d["time"]) if isinstance(d["time"], str) else int(d["time"]))
        if i is None:
            continue                                     # not a candle in this series
        if i >= len(candles) - 1:
            unresolved += 1                              # fired on the final bar
            continue
        direction = "LONG" if d["event"] == "break_up" else "SHORT"
        entry, line = float(d["price"]), float(d["line"])
        risk = abs(entry - line)
        if risk <= 0:
            continue
        target = entry + rr * risk if direction == "LONG" else entry - rr * risk
        kind, exit_price, bars, ambiguous = _outcome_of(
            candles, i, direction, entry, line, target, horizon)
        if kind in ("none", "truncated"):
            unresolved += 1
            continue
        r = analytics.r_multiple(direction, entry, line, exit_price)
        if r is None:
            # Should not happen for engine-generated signals (a break_up sits above
            # its line), but an imported one could invert it. Counting it as judged
            # while analytics drops it would make n disagree with the tallies.
            unresolved += 1
            continue
        rows.append({
            "symbol": d["symbol"], "tf": d["tf"], "time": d["time"], "event": d["event"],
            "direction": direction, "entry": entry, "line": line, "target": target,
            "exit": exit_price, "exit_kind": kind, "bars_held": bars,
            "ambiguous": ambiguous, "r_multiple": r,
            "outcome": analytics.derive_outcome(r),
            "rsi": d.get("rsi"), "atr_dist": d.get("atr_dist"), "touches": d.get("touches"),
        })
    return rows, unresolved


def _exit_ts(row: dict) -> int:
    """When the trade actually closed. ``analytics.closed()`` orders by ``closed_ts``
    to build the equity curve, so using the entry time would sequence a signal that
    ran 30 bars before one that stopped out the next day — a drawdown over an order
    that never happened."""
    opened = iso_to_ms(row["time"])
    try:
        return opened + row["bars_held"] * bar_seconds(row["tf"]) * 1000
    except (ValueError, TypeError):
        return opened                      # unknown bar string: no better estimate


def as_trades(rows: list[dict]) -> list[Trade]:
    """Synthetic closed trades so ``analytics`` produces every statistic."""
    return [
        Trade(id=i + 1, symbol=r["symbol"], direction=r["direction"], status="CLOSED",
              created_ts=0, updated_ts=0, tf=r["tf"], entry_price=r["entry"],
              sl_price=r["line"], exit_price=r["exit"], outcome=r["outcome"],
              r_multiple=r["r_multiple"], opened_ts=iso_to_ms(r["time"]),
              closed_ts=_exit_ts(r))
        for i, r in enumerate(rows)
    ]


def summarize(rows: list[dict]) -> dict:
    return analytics.summarize(as_trades(rows))


def _window(rows: list[dict]) -> str:
    """Fallback when no candle window was recorded: the span of the signals."""
    if not rows:
        return "–"
    times = sorted(r["time"] for r in rows)
    return f"{times[0][:10]} → {times[-1][:10]}"


def candle_window(candles: Candles, warmup: int = 0, horizon: int = 0) -> tuple[int, int] | None:
    """The period over which signals could actually be sampled: from the first bar
    after the warm-up to the last bar that still has a full horizon behind it.

    Comparability is judged on this rather than on the span of the signals, which
    two markets can share while firing their first and last signals months apart —
    and rather than on the raw candle range, whose warm-up padding is a fixed
    *bar* count and so covers 90 days on 1D but 15 on 4H.
    """
    n = len(candles)
    if n == 0:
        return None
    first = min(warmup, n - 1)
    last = max(first, n - 1 - horizon)
    return int(candles.ts[first]), int(candles.ts[last])


def _fmt_window(w: tuple[int, int] | None) -> str:
    if not w:
        return "–"
    def day(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return f"{day(w[0])} → {day(w[1])}"


def windows_comparable(windows: list[tuple[int, int]], min_overlap: float = 0.9) -> bool:
    """True when every window overlaps the others by at least ``min_overlap`` of the
    union — so a few days of padding difference does not cry wolf, but comparing
    three years of 1D against six months of 4H does."""
    ws = [w for w in windows if w]
    if len(ws) < 2:
        return True
    lo, hi = max(w[0] for w in ws), min(w[1] for w in ws)
    overlap = max(0, hi - lo)
    union = max(w[1] for w in ws) - min(w[0] for w in ws)
    return union > 0 and overlap / union >= min_overlap


def render(per_market: list[dict], rr: float, horizon: int,
           params_label: str = "strict defaults") -> str:
    """Markdown: one line per market, then the pooled result.

    Each row carries its own window, because a bar count covers a different span
    on every timeframe — 1100 bars is ~3 years of 1D but ~6 months of 4H. Rows
    whose windows differ are not comparable, and the pooled line says so.
    """
    out = [f"# Backtest hit-rate — target {rr:g}R, stop at the line, {horizon}-bar horizon", "",
           f"Signals generated with **{params_label}**; a stop fills at the worse of the "
           f"stop and the bar's open, a target at the limit.", ""]
    out.append("| market | window | signals | W/L/BE | win | avg R | total R | PF | max DD | ambig | unresolved |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")

    def row(label: str, rows_: list[dict], window: str, unresolved: int) -> str:
        s = summarize(rows_)
        amb = sum(1 for r in rows_ if r["ambiguous"])
        win = "–" if s["win_rate"] is None else f"{round(s['win_rate'] * 100)}%"
        return (f"| {label} | {window} | {s['n']} | {s['wins']}/{s['losses']}/{s['be']} | {win} | "
                f"{_n(s['avg_r'])} | {_n(s['total_r'])} | {_n(s['profit_factor'])} | "
                f"{_n(s['max_drawdown_r'])} | {amb} | {unresolved} |")

    pooled: list[dict] = []
    unresolved_total = 0
    windows: list[tuple[int, int]] = []
    for m in per_market:
        w = m.get("window")
        label = _fmt_window(w) if w else _window(m["rows"])
        out.append(row(f"{m['symbol']} {m['tf']}", m["rows"], label, m["unresolved"]))
        pooled += m["rows"]
        unresolved_total += m["unresolved"]
        if w:
            windows.append(w)
    comparable = windows_comparable(windows)
    if comparable and windows:
        pooled_window = _fmt_window((max(w[0] for w in windows), min(w[1] for w in windows)))
    elif comparable:
        pooled_window = _window(pooled)
    else:
        pooled_window = "mixed"
    out.append(row("**all**", pooled, pooled_window, unresolved_total))
    out.append("")
    if not comparable:
        out.append("> **The rows above cover different windows, so they are not directly "
                   "comparable and the pooled row mixes periods.** A bar count spans a "
                   "different amount of time on each timeframe; pass `--days` instead of "
                   "`--limit` to give every timeframe the same window.")
        out.append("")
    if unresolved_total:
        out.append(f"{unresolved_total} signal(s) fired too close to the end of the data to "
                   f"resolve within {horizon} bars and are excluded rather than counted flat.")

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
        "- **One window.** Each row covers the period in its window column — one regime. A",
        "  run over a shorter window usually just caught a friendlier one, so compare rows",
        "  only when their windows match (`--days` makes them match across timeframes;",
        "  `--limit` does not, because a bar count spans less time on a faster timeframe).",
        "- **Sample size.** Per-market counts are in the table; treat anything under ~30",
        "  signals as indicative only.",
        "- **Engine parameters.** The header says which produced these signals. Without",
        "  `--config` they are the strict library defaults, not whatever your config.yaml",
        "  tunes, so the report would describe a system you are not running.",
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


def bars_for(tf: str, days: int, warmup: int, horizon: int) -> int:
    """Bars needed to cover ``days`` of signals on ``tf``, plus the warm-up the
    replay consumes before it can fire anything and the horizon the last signal
    needs. This is what makes two timeframes comparable."""
    return int(days * 86_400 / bar_seconds(tf)) + warmup + horizon


def load_params(config_path: str | None) -> tuple[Params, str, str | None]:
    """``(params, label, exchange)``. Without a config this is the strict library
    default, which is then what the report says — the numbers must never look like
    they describe the tuned engine the reader is running when they do not."""
    if not config_path:
        return Params(), "strict defaults", None
    cfg = load_config(config_path)
    params = cfg.to_params()
    label = f"{config_path}" if params != Params() else f"{config_path} (= strict defaults)"
    return params, label, cfg.exchange


async def _main(args) -> int:
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    tfs = [t.strip() for t in args.tf.split(",") if t.strip()]
    params, params_label, cfg_exchange = load_params(args.config)
    rest = data_rest(args.exchange or cfg_exchange or "okx")
    per_market: list[dict] = []
    failed: list[str] = []

    async with aiohttp.ClientSession() as session:
        for symbol in symbols:
            for tf in tfs:
                limit = bars_for(tf, args.days, args.warmup, args.horizon) if args.days else args.limit
                try:
                    candles = await rest.fetch_candles(session, symbol, tf, limit)
                except Exception as e:  # noqa: BLE001 — one bad market must not lose the rest
                    failed.append(f"{symbol} {tf}")
                    print(f"{symbol} {tf}: FAILED ({e.__class__.__name__}: {e}) — skipped",
                          file=sys.stderr)
                    continue
                if len(candles) < args.warmup + 5:
                    failed.append(f"{symbol} {tf}")
                    print(f"{symbol} {tf}: only {len(candles)} candles, skipped", file=sys.stderr)
                    continue
                signals = replay_signals(candles, params, tf, symbol, args.warmup)
                rows, unresolved = evaluate(candles, signals, args.rr, args.horizon)
                per_market.append({"symbol": symbol, "tf": tf, "rows": rows,
                                   "unresolved": unresolved,
                                   "window": candle_window(candles, args.warmup, args.horizon)})
                print(f"{symbol} {tf}: {len(candles)} candles ({limit} asked), "
                      f"{len(signals)} signals, {len(rows)} judged, {unresolved} unresolved",
                      file=sys.stderr)

    if not per_market:
        print("nothing to report", file=sys.stderr)
        return 1
    if failed:
        print(f"note: {len(failed)} market(s) missing from the report: {', '.join(failed)}",
              file=sys.stderr)

    text = render(per_market, args.rr, args.horizon, params_label)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([{"symbol": m["symbol"], "tf": m["tf"], "unresolved": m["unresolved"],
                        "signals": m["rows"]} for m in per_market], fh, indent=1)
        print(f"wrote {args.json}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbols", default="SOL-USDT-SWAP,BTC-USDT-SWAP,ETH-USDT-SWAP")
    ap.add_argument("--tf", default="1D", help="comma-separated, e.g. 1D,4H")
    ap.add_argument("-c", "--config", default=None,
                    help="config.yaml to take the engine params (and exchange) from; "
                         "without it the strict library defaults are used and the report says so")
    ap.add_argument("--exchange", default=None, choices=["okx", "binance"],
                    help="overrides the config's exchange (default okx)")
    ap.add_argument("--days", type=int, default=None,
                    help="cover this many days of signals on EVERY timeframe (recommended "
                         "when comparing timeframes; overrides --limit)")
    ap.add_argument("--limit", type=int, default=400,
                    help="candles per market — note this is a bar count, so it spans a "
                         "different period on each timeframe")
    ap.add_argument("--warmup", type=int, default=60)
    ap.add_argument("--rr", type=float, default=DEFAULT_RR, help="target in R")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON, help="bars to resolve a signal")
    ap.add_argument("--out", default=None, help="write markdown here instead of stdout")
    ap.add_argument("--json", default=None, help="also dump the per-signal rows")
    args = ap.parse_args(argv)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
