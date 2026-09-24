"""OKX V5 live candle WebSocket — yields only confirmed (closed) candles.

Candle channels live on the *business* endpoint::

    wss://ws.okx.com:8443/ws/v5/business

Subscribe with ``{"channel": "candle" + tf, "instId": symbol}`` (e.g.
``candle1D``). Each push carries the same array shape as REST::

    [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]

The forming bar streams with ``confirm == "0"`` and flips to ``"1"`` on close;
we emit a :class:`ClosedCandle` only on that flip, so downstream sees each bar
exactly once and never acts intrabar.

Heartbeat: OKX drops the socket after ~30s of silence. We send the literal text
``"ping"`` after 20s idle and OKX replies ``"pong"`` (both plain text, not JSON).
Disconnects reconnect with exponential backoff. If even the pings stop being
answered the socket is treated as dead after :data:`IDLE_TIMEOUT_S` and
reconnected — a connected-but-mute stream is otherwise indistinguishable from a
quiet market, and nothing would ever raise.

Set ``OKX_WS_URL`` to use a regional host if the default is geo-blocked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator

import websockets

log = logging.getLogger(__name__)

WS_URL = os.environ.get("OKX_WS_URL", "wss://ws.okx.com:8443/ws/v5/business")

_IDLE_PING_S = 20.0   # send "ping" after this many seconds without a message
_MAX_BACKOFF_S = 60.0
# A ping that is never answered must not keep the loop alive forever: without
# this the socket stays "connected" and mute, and the watcher waits for a candle
# that will never come. Counted from the last message of ANY kind, pong included.
IDLE_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class ClosedCandle:
    ts: int          # candle open time, epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float


def _to_candle(a: list) -> ClosedCandle:
    return ClosedCandle(
        ts=int(a[0]),
        open=float(a[1]),
        high=float(a[2]),
        low=float(a[3]),
        close=float(a[4]),
        volume=float(a[5]),
    )


async def stream_closed_candles(symbol: str, tf: str) -> AsyncIterator[ClosedCandle]:
    """Yield each closed candle for ``symbol``/``tf`` as it confirms.

    Runs forever, reconnecting on any drop. ``symbol`` is an OKX instId; ``tf``
    is an OKX bar string that maps directly to the ``candle<tf>`` channel.
    """
    channel = f"candle{tf}"
    sub = json.dumps(
        {"op": "subscribe", "args": [{"channel": channel, "instId": symbol}]}
    )
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None, max_queue=None) as ws:
                await ws.send(sub)
                backoff = 1.0  # connected cleanly — reset backoff
                log.info("subscribed %s %s", channel, symbol)

                last_msg = time.monotonic()
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=_IDLE_PING_S)
                    except asyncio.TimeoutError:
                        if time.monotonic() - last_msg > IDLE_TIMEOUT_S:
                            raise ConnectionError(
                                f"silent for {IDLE_TIMEOUT_S:.0f}s despite pings — "
                                "stream is dead") from None
                        await ws.send("ping")  # keep the socket alive
                        continue

                    last_msg = time.monotonic()
                    if raw == "pong":
                        continue

                    msg = json.loads(raw)
                    if "event" in msg:  # subscribe ack / error notice
                        if msg["event"] == "error":
                            log.error("okx ws error: %s", msg)
                        continue

                    for a in msg.get("data", []):
                        confirm = a[8] if len(a) > 8 else "0"
                        if confirm == "1":
                            yield _to_candle(a)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect on any transport error
            log.warning("%s %s ws dropped (%s); reconnecting in %.0fs",
                        channel, symbol, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
