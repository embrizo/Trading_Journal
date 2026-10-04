"""OKX V5 REST candle backfill.

Paged, oldest-first, confirmed candles only — the same information the live
watcher will see, so backtests carry no look-ahead.

OKX returns candles **newest-first** as arrays of strings::

    [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]

``confirm`` is ``"1"`` once the bar has closed. We keep only closed bars (the
current forming bar is dropped) and reverse to oldest-first before building the
column-oriented :class:`~break_signal.core.types.Candles`.

Two endpoints are used:

* ``/api/v5/market/candles``          — most recent, up to 300 per call.
* ``/api/v5/market/history-candles``  — older data, up to 100 per call, paged
  backwards with ``after`` (returns records strictly older than that ts).

No API key is required for public market data. Set ``OKX_REST_URL`` to switch to
a regional host (e.g. ``https://aws.okx.com``) if the default is geo-blocked.
"""
from __future__ import annotations

import logging
import os

import aiohttp
import numpy as np

from ..core.types import Candles

log = logging.getLogger(__name__)

REST_URL = os.environ.get("OKX_REST_URL", "https://www.okx.com").rstrip("/")

_CANDLES = "/api/v5/market/candles"
_HISTORY = "/api/v5/market/history-candles"
_TICKERS = "/api/v5/market/tickers"
_CANDLES_MAX = 300   # OKX per-call cap for /candles
_HISTORY_MAX = 100   # OKX per-call cap for /history-candles
_TIMEOUT = aiohttp.ClientTimeout(total=30)


class OkxError(RuntimeError):
    """Non-zero ``code`` in an OKX REST response."""


async def _get(session: aiohttp.ClientSession, path: str, params: dict) -> list[list]:
    async with session.get(REST_URL + path, params=params, timeout=_TIMEOUT) as resp:
        resp.raise_for_status()
        body = await resp.json()
    if body.get("code") != "0":
        raise OkxError(f"{path} -> code={body.get('code')} msg={body.get('msg')!r}")
    return body.get("data", []) or []


def _absorb(rows: dict[int, tuple], data: list[list], *, confirmed_only: bool) -> int:
    """Merge raw OKX rows into ``rows`` keyed by open-time. Returns new count."""
    added = 0
    for a in data:
        # confirm flag is the last element; history rows are always closed.
        confirm = a[8] if len(a) > 8 else "1"
        if confirmed_only and confirm != "1":
            continue
        ts = int(a[0])
        if ts in rows:
            continue
        rows[ts] = (float(a[1]), float(a[2]), float(a[3]), float(a[4]), float(a[5]))
        added += 1
    return added


async def fetch_tickers(
    session: aiohttp.ClientSession, inst_type: str = "SWAP"
) -> dict[str, dict]:
    """Last price + 24h change for every ``inst_type`` instrument, in one call.

    Returns ``{instId: {"last": float, "change24h": float | None}}`` where
    ``change24h`` is the percentage move from the 24h-ago open (``None`` when
    OKX reports a zero/absent open). Used by the dashboard watchlist and the
    one-shot price alerts — no API key needed (public market data).
    """
    data = await _get(session, _TICKERS, {"instType": inst_type})
    out: dict[str, dict] = {}
    for t in data:
        try:
            inst = t["instId"]
            last = float(t["last"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            open24 = float(t.get("open24h") or 0)
        except (TypeError, ValueError):
            open24 = 0.0
        change = ((last - open24) / open24 * 100.0) if open24 else None
        out[inst] = {"last": last, "change24h": change}
    return out


async def fetch_candles(
    session: aiohttp.ClientSession, symbol: str, tf: str, limit: int
) -> Candles:
    """Fetch up to ``limit`` closed OHLCV candles for ``symbol``/``tf``.

    ``symbol`` is an OKX instId (e.g. ``SOL-USDT-SWAP``); ``tf`` is an OKX bar
    string (``1D``, ``4H``, ...). The returned :class:`Candles` is oldest-first
    and holds at most the ``limit`` most recent closed bars.
    """
    rows: dict[int, tuple] = {}

    # First page: the recent window (includes the current, unconfirmed bar).
    first = await _get(
        session, _CANDLES,
        {"instId": symbol, "bar": tf, "limit": str(min(limit, _CANDLES_MAX))},
    )
    _absorb(rows, first, confirmed_only=True)

    # Page backwards through history until we have enough or run dry.
    while len(rows) < limit:
        oldest = min(rows) if rows else None
        params = {"instId": symbol, "bar": tf, "limit": str(_HISTORY_MAX)}
        if oldest is not None:
            params["after"] = str(oldest)  # records strictly older than this ts
        data = await _get(session, _HISTORY, params)
        if not data or _absorb(rows, data, confirmed_only=True) == 0:
            break

    if not rows:
        log.warning("no candles returned for %s %s", symbol, tf)
        empty_i = np.empty(0, dtype=np.int64)
        empty_f = np.empty(0, dtype=np.float64)
        return Candles(empty_i, empty_f.copy(), empty_f.copy(), empty_f.copy(),
                       empty_f.copy(), empty_f.copy())

    items = sorted(rows.items())[-limit:]  # oldest-first, keep most recent `limit`
    ts = np.fromiter((t for t, _ in items), dtype=np.int64, count=len(items))
    cols = np.array([v for _, v in items], dtype=np.float64)  # shape (n, 5)
    return Candles(
        ts=ts,
        open=cols[:, 0].copy(),
        high=cols[:, 1].copy(),
        low=cols[:, 2].copy(),
        close=cols[:, 3].copy(),
        volume=cols[:, 4].copy(),
    )
