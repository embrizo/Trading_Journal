"""Binance USDⓈ-M futures REST candle backfill.

Same contract as :mod:`okx_rest`: oldest-first, **closed candles only**, so a
backtest sees exactly what the live watcher will.

Binance returns klines oldest-first as arrays::

    [openTime, open, high, low, close, volume, closeTime, ...]

There is no ``confirm`` flag — a bar is closed when its ``closeTime`` has passed,
so the forming bar is dropped by comparing against the server clock. One call
returns up to 1500 rows; older data is paged backwards with ``endTime``.

Symbols are the project's canonical OKX-style instIds (``SOL-USDT-SWAP``) and are
translated here, so the journal, the config and the Pine script keep one naming
scheme whichever exchange the data comes from.

No API key is required for public market data. Set ``BINANCE_REST_URL`` to switch
host (e.g. ``https://data-api.binance.vision`` for the spot data mirror).
"""
from __future__ import annotations

import logging
import os
import time

import aiohttp
import numpy as np

from ..core.types import Candles

log = logging.getLogger(__name__)

REST_URL = os.environ.get("BINANCE_REST_URL", "https://fapi.binance.com").rstrip("/")

_KLINES = "/fapi/v1/klines"
_MAX = 1500          # Binance per-call cap
_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Project timeframe → Binance interval. Binance is lower-case for everything
# except 1M (month), which would otherwise collide with 1m (minute).
_INTERVALS = {
    "1M": "1m", "3M": "3m", "5M": "5m", "15M": "15m", "30M": "30m",
    "1H": "1h", "2H": "2h", "4H": "4h", "6H": "6h", "8H": "8h", "12H": "12h",
    "1D": "1d", "3D": "3d", "1W": "1w",
}


class BinanceError(RuntimeError):
    """Binance replied with an error payload."""


def to_symbol(instid: str) -> str:
    """``SOL-USDT-SWAP`` → ``SOLUSDT``; ``SOL-USDT`` → ``SOLUSDT``; passthrough otherwise."""
    s = instid.strip().upper()
    for suffix in ("-SWAP", "-PERP", "-FUTURES"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s.replace("-", "")


def to_interval(tf: str) -> str:
    """Project bar string → Binance interval. Minutes are written ``15m`` here and
    ``15M`` in some configs, so both spellings resolve."""
    t = tf.strip()
    if t in ("1m", "3m", "5m", "15m", "30m"):     # already Binance-shaped minutes
        return t
    up = t.upper()
    if up in _INTERVALS:
        return _INTERVALS[up]
    raise ValueError(f"no Binance interval for timeframe {tf!r}")


async def _get(session: aiohttp.ClientSession, params: dict) -> list[list]:
    async with session.get(REST_URL + _KLINES, params=params, timeout=_TIMEOUT) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise BinanceError(f"{_KLINES} -> HTTP {resp.status}: {body[:200]}")
        data = await resp.json()
    if isinstance(data, dict):                     # {"code": -1121, "msg": "Invalid symbol."}
        raise BinanceError(f"{_KLINES} -> code={data.get('code')} msg={data.get('msg')!r}")
    return data or []


def _absorb(rows: dict[int, tuple], data: list[list], now_ms: int) -> int:
    """Merge klines into ``rows`` keyed by open time, skipping the forming bar."""
    added = 0
    for a in data:
        if int(a[6]) >= now_ms:                    # closeTime in the future → still forming
            continue
        ts = int(a[0])
        if ts in rows:
            continue
        rows[ts] = (float(a[1]), float(a[2]), float(a[3]), float(a[4]), float(a[5]))
        added += 1
    return added


async def fetch_candles(
    session: aiohttp.ClientSession, symbol: str, tf: str, limit: int
) -> Candles:
    """Fetch up to ``limit`` closed OHLCV candles, oldest-first.

    ``symbol`` is a canonical instId (``SOL-USDT-SWAP``) and ``tf`` a project bar
    string (``1D``, ``4H``); both are translated to Binance's spelling.
    """
    sym, interval = to_symbol(symbol), to_interval(tf)
    now_ms = int(time.time() * 1000)
    rows: dict[int, tuple] = {}

    first = await _get(session, {"symbol": sym, "interval": interval,
                                 "limit": str(min(limit + 1, _MAX))})
    _absorb(rows, first, now_ms)

    while len(rows) < limit:
        oldest = min(rows) if rows else None
        params = {"symbol": sym, "interval": interval, "limit": str(_MAX)}
        if oldest is not None:
            params["endTime"] = str(oldest - 1)     # strictly older than what we have
        data = await _get(session, params)
        if not data or _absorb(rows, data, now_ms) == 0:
            break

    if not rows:
        log.warning("no candles returned for %s %s (binance %s %s)", symbol, tf, sym, interval)
        empty_i = np.empty(0, dtype=np.int64)
        empty_f = np.empty(0, dtype=np.float64)
        return Candles(empty_i, empty_f.copy(), empty_f.copy(), empty_f.copy(),
                       empty_f.copy(), empty_f.copy())

    items = sorted(rows.items())[-limit:]
    ts = np.fromiter((t for t, _ in items), dtype=np.int64, count=len(items))
    cols = np.array([v for _, v in items], dtype=np.float64)
    return Candles(
        ts=ts,
        open=cols[:, 0].copy(),
        high=cols[:, 1].copy(),
        low=cols[:, 2].copy(),
        close=cols[:, 3].copy(),
        volume=cols[:, 4].copy(),
    )
