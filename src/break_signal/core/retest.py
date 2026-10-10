"""Break-and-retest detection on the confirmed candle.

A retest is the second leg of the pattern: after a line breaks (handled by
``breakout.check_breaks``), price pulls back to the now-flipped line, tags it
with a wick, and *holds* — closing back on the breakout side. The broken line's
geometry is frozen at break time (``PendingRetest``) and recomputed at each
later bar, so this module never touches ``build_lines`` and is robust to the
anchor pivots aging out of the window.

Off by default (``Params.use_retest``). Mirrors ``breakout.py``: pure, no I/O,
called once per *confirmed* candle. See ``RETEST_SIGNAL_PLAN.md``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .params import Params
from .types import Candles, Signal, ms_to_iso


@dataclass(frozen=True)
class PendingRetest:
    """A broken line being watched for a retest, with its geometry frozen.

    ``slope`` is price-per-bar (same as ``Trendline.slope``). ``ts_ref`` is the
    break candle's open time (ms) and ``value_ref`` is the line value there, so
    the line value at any later candle is recomputed from timestamps, immune to
    the anchor pivots dropping out of the window.
    """

    line_id: str          # "R:ts_a:ts_b" — the broken line's id
    side: str             # "R" (resistance break-up) | "S" (support break-down)
    ts_ref: int           # break candle open time (ms)
    value_ref: float      # line value at the break candle
    slope: float          # price per bar
    touches: int
    broke_ts: int         # break candle open time (ms); == ts_ref at creation

    def value_at(self, ts_ms: int, tf_seconds: int) -> float:
        bars = (ts_ms - self.ts_ref) / (tf_seconds * 1000)
        return self.value_ref + self.slope * bars


@dataclass
class RetestResult:
    """Output of one ``check_retests`` call at a single confirmed bar."""

    signals: list[Signal] = field(default_factory=list)
    # (line_id, reason) with reason in {"retested", "invalidated", "expired"}.
    retire: list[tuple[str, str]] = field(default_factory=list)


def _anchor_ts(line_id: str) -> int | None:
    """Parse ts_a (the older anchor's open time) out of a line id."""
    try:
        return int(line_id.split(":")[1])
    except (IndexError, ValueError):
        return None


def check_retests(
    candles: Candles,
    pending: list[PendingRetest],
    atr_last: float,
    rsi_last: float,
    params: Params,
    last_bar: int,
    tf_seconds: int,
    symbol: str,
    exchange: str,
    tf: str,
) -> RetestResult:
    """Resolve each pending retest against the confirmed candle at ``last_bar``.

    Only ever call this for a *confirmed* (closed) candle. Each pending is
    checked once per bar: it either fires a retest signal, is invalidated (the
    breakout failed), expires (no retest within ``retest_window``), or carries
    over untouched.
    """
    result = RetestResult()
    if not pending or not np.isfinite(atr_last) or atr_last <= 0:
        return result

    ts = int(candles.ts[last_bar])
    c = float(candles.close[last_bar])
    h = float(candles.high[last_bar])
    lo = float(candles.low[last_bar])
    touch = params.retest_touch * atr_last
    hold_buf = params.retest_hold_buf * atr_last
    invalidate = params.retest_invalidate * atr_last
    mid = (h + lo) / 2

    for p in pending:
        age = round((ts - p.broke_ts) / (tf_seconds * 1000))
        if age <= 0:
            continue  # same bar as the break (or before) — nothing to retest yet
        if age > params.retest_window:
            # Window elapsed with no retest or invalidation — the move ran away.
            result.retire.append((p.line_id, "expired"))
            continue

        lv = p.value_at(ts, tf_seconds)

        if p.side == "R":  # resistance broke up → look for a hold above
            tagged = lo <= lv + touch
            held = c >= lv + hold_buf
            invalidated = c < lv - invalidate
        else:              # support broke down → look for a hold below
            tagged = h >= lv - touch
            held = c <= lv - hold_buf
            invalidated = c > lv + invalidate

        reject_ok = (not params.retest_reject_body) or (
            c >= mid if p.side == "R" else c <= mid
        )

        if tagged and held and reject_ok:
            anchor = _anchor_ts(p.line_id)
            age_bars = (
                round((ts - anchor) / (tf_seconds * 1000)) if anchor is not None else age
            )
            result.signals.append(
                Signal(
                    symbol=symbol,
                    exchange=exchange,
                    tf=tf,
                    event="retest_up" if p.side == "R" else "retest_down",
                    side="resistance" if p.side == "R" else "support",
                    price=c,
                    line=lv,
                    atr_dist=abs(c - lv) / atr_last,
                    touches=p.touches,
                    age_bars=age_bars,
                    vol_ratio=0.0,  # no volume filter on the retest leg (§7.2)
                    rsi=rsi_last if math.isfinite(rsi_last) else 0.0,
                    time=ms_to_iso(ts),
                    line_id=p.line_id,
                )
            )
            result.retire.append((p.line_id, "retested"))
        elif invalidated:
            result.retire.append((p.line_id, "invalidated"))
        # else: neither this bar — carry the pending over to the next.

    return result
