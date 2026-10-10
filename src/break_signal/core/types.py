"""Shared data structures for the core algorithm."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1

import numpy as np


@dataclass
class Candles:
    """Column-oriented OHLCV series, oldest first, indexed by bar.

    Bar index ``i`` is the position in this array; the "current" bar is the
    last one (``len(self) - 1``). ``ts`` is the candle open time in epoch ms.
    """

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __len__(self) -> int:  # noqa: D105
        return int(self.close.shape[0])

    def slice(self, start: int) -> "Candles":
        return Candles(
            ts=self.ts[start:],
            open=self.open[start:],
            high=self.high[start:],
            low=self.low[start:],
            close=self.close[start:],
            volume=self.volume[start:],
        )


@dataclass(frozen=True)
class Trendline:
    """A support or resistance line anchored on two confirmed pivots."""

    side: str          # "R" (resistance) or "S" (support)
    ax: int            # anchor A bar index (older)
    ay: float          # anchor A price
    bx: int            # anchor B bar index (newer)
    by: float          # anchor B price
    slope: float       # price per bar
    touches: int
    score: float
    ts_a: int          # anchor A open time (ms) — stable id across restarts
    ts_b: int          # anchor B open time (ms)

    def value_at(self, bar: int) -> float:
        return self.ay + self.slope * (bar - self.ax)

    @property
    def id(self) -> str:
        # Keyed on timestamps (not bar indices) so the id survives a restart
        # where the absolute bar index would shift.
        return f"{self.side}:{self.ts_a}:{self.ts_b}"


@dataclass
class Signal:
    """A confirmed breakout, ready to serialize into an alert payload."""

    symbol: str
    exchange: str
    tf: str
    event: str         # "break_up" | "break_down" | "retest_up" | "retest_down"
    side: str          # "resistance" | "support"
    price: float
    line: float
    atr_dist: float
    touches: int
    age_bars: int
    vol_ratio: float
    rsi: float
    time: str          # ISO8601 UTC of the breaking candle's open
    line_id: str = ""
    _line: Trendline | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "tf": self.tf,
            "event": self.event,
            "side": self.side,
            "price": round(self.price, 6),
            "line": round(self.line, 6),
            "atr_dist": round(self.atr_dist, 2),
            "touches": self.touches,
            "age_bars": self.age_bars,
            "vol_ratio": round(self.vol_ratio, 2),
            "rsi": round(self.rsi, 1),
            "time": self.time,
        }


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
