"""Binance USDⓈ-M futures live kline WebSocket — yields only closed candles.

Same contract as :mod:`okx_ws`, including the :class:`ClosedCandle` type, which
is imported from there so the watcher does not care which exchange it is on.

Stream: ``wss://fstream.binance.com/ws/<symbol>@kline_<interval>`` (symbol lower
case). Each push carries::

    {"e": "kline", "k": {"t": openTime, "o": .., "h": .., "l": .., "c": ..,
                         "v": volume, "x": isClosed}}

``x`` flips to ``true`` on the close, and only then is a candle emitted — so a
bar is seen exactly once and never acted on intrabar.

Heartbeat: Binance sends WebSocket ping frames every few minutes and the client
library answers them automatically, so there is no text ping to send (OKX needs
one; Binance does not). Disconnects reconnect with exponential backoff.

Set ``BINANCE_WS_URL`` to use a different host.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator

import websockets

from .binance_rest import to_interval, to_symbol
from .okx_ws import ClosedCandle

log = logging.getLogger(__name__)

WS_URL = os.environ.get("BINANCE_WS_URL", "wss://fstream.binance.com").rstrip("/")

_MAX_BACKOFF_S = 60.0


def _to_candle(k: dict) -> ClosedCandle:
    return ClosedCandle(
        ts=int(k["t"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
    )


async def stream_closed_candles(symbol: str, tf: str) -> AsyncIterator[ClosedCandle]:
    """Yield each closed candle for ``symbol``/``tf`` as it closes.

    Runs forever, reconnecting on any drop. ``symbol`` is a canonical instId
    (``SOL-USDT-SWAP``); it and ``tf`` are translated to Binance's spelling.
    """
    stream = f"{to_symbol(symbol).lower()}@kline_{to_interval(tf)}"
    url = f"{WS_URL}/ws/{stream}"
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(url, max_queue=None) as ws:
                backoff = 1.0
                log.info("subscribed %s", stream)
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    k = msg.get("k")
                    if k and k.get("x"):
                        yield _to_candle(k)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect on any transport error
            log.warning("%s ws dropped (%s); reconnecting in %.0fs", stream, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
