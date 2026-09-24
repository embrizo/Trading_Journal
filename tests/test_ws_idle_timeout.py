"""A connected-but-mute WebSocket must be treated as dead and reconnected.

This is the failure that hid the wrong Binance path for a whole session: the
socket opened, logged "subscribed", and then delivered nothing for ever, with no
error anywhere. Both stream modules now give up after a period of silence.
"""
import asyncio

import pytest

from break_signal.data import binance_ws as BW
from break_signal.data import okx_ws as OW


class FakeWS:
    """Serves a scripted list of messages, then goes silent for ever.

    ``gap`` is how long each message takes to arrive. It matters: with everything
    delivered instantly a test cannot tell a per-message deadline from one measured
    since connect, which is the very bug these tests exist to catch.
    """

    def __init__(self, messages, sent=None, gap=0.0):
        self.messages = list(messages)
        self.sent = sent if sent is not None else []
        self.gap = gap

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        if self.messages:
            if self.gap:
                await asyncio.sleep(self.gap)
            return self.messages.pop(0)
        await asyncio.sleep(3600)          # mute: the caller's timeout must fire

    async def send(self, text):
        self.sent.append(text)


def _connector(sockets):
    """websockets.connect stand-in handing out one FakeWS per connection."""
    made = []

    def connect(url, **kw):
        made.append(url)
        return sockets[min(len(made) - 1, len(sockets) - 1)]

    connect.urls = made
    return connect


async def _first_candle(agen, timeout=5.0):
    async def go():
        async for c in agen:
            return c
    try:
        return await asyncio.wait_for(go(), timeout)
    finally:
        await agen.aclose()


KLINE = '{"e":"kline","k":{"t":1000,"o":"1","h":"2","l":"0.5","c":"1.5","v":"10","x":true}}'
OKX_MSG = '{"data":[["1000","1","2","0.5","1.5","10","0","0","1"]]}'


# ── binance ──────────────────────────────────────────────────────────────────
def test_binance_reconnects_after_silence_and_then_delivers(monkeypatch, caplog):
    dead = FakeWS([])                       # connects, says nothing, ever
    alive = FakeWS([KLINE])
    connect = _connector([dead, alive])
    monkeypatch.setattr(BW.websockets, "connect", connect)
    monkeypatch.setattr(BW, "IDLE_TIMEOUT_S", 0.05)

    async def go():
        with caplog.at_level("WARNING"):
            return await _first_candle(BW.stream_closed_candles("SOL-USDT-SWAP", "1D"))

    candle = asyncio.run(go())
    assert candle.close == 1.5 and candle.ts == 1000
    assert len(connect.urls) == 2                       # gave up on the mute one
    assert "silent for" in caplog.text and "dead" in caplog.text
    assert "/market/ws/solusdt@kline_1d" in connect.urls[0]


def test_binance_does_not_time_out_a_talking_stream(monkeypatch):
    """Four messages 0.1s apart under a 0.25s deadline: fine per message, but well
    past it in total. A deadline measured from connect would kill this stream."""
    chatty = FakeWS(['{"e":"kline","k":{"t":1,"x":false}}'] * 3 + [KLINE], gap=0.1)
    connect = _connector([chatty])
    monkeypatch.setattr(BW.websockets, "connect", connect)
    monkeypatch.setattr(BW, "IDLE_TIMEOUT_S", 0.25)

    candle = asyncio.run(_first_candle(BW.stream_closed_candles("SOL-USDT-SWAP", "1D")))
    assert candle.ts == 1000
    assert len(connect.urls) == 1                       # never dropped and reconnected


# ── okx ──────────────────────────────────────────────────────────────────────
def test_okx_pings_first_then_gives_up(monkeypatch, caplog):
    sent = []
    dead = FakeWS([], sent)                 # never answers, not even "pong"
    alive = FakeWS([OKX_MSG])
    connect = _connector([dead, alive])
    monkeypatch.setattr(OW.websockets, "connect", connect)
    monkeypatch.setattr(OW, "_IDLE_PING_S", 0.02)
    monkeypatch.setattr(OW, "IDLE_TIMEOUT_S", 0.10)

    async def go():
        with caplog.at_level("WARNING"):
            return await _first_candle(OW.stream_closed_candles("SOL-USDT-SWAP", "1D"))

    candle = asyncio.run(go())
    assert candle.close == 1.5
    assert sent.count("ping") >= 2                      # tried to revive it first
    assert len(connect.urls) == 2
    assert "despite pings" in caplog.text


def test_okx_pong_keeps_the_stream_alive(monkeypatch):
    """Answered pings hold the socket open indefinitely: three messages 0.1s apart
    under a 0.25s deadline, i.e. 0.3s in total, and it must survive — a pong has to
    count as a sign of life, not just a candle."""
    sent = []
    ws = FakeWS(["pong", "pong", OKX_MSG], sent, gap=0.1)
    connect = _connector([ws])
    monkeypatch.setattr(OW.websockets, "connect", connect)
    monkeypatch.setattr(OW, "_IDLE_PING_S", 0.2)
    monkeypatch.setattr(OW, "IDLE_TIMEOUT_S", 0.25)

    candle = asyncio.run(_first_candle(OW.stream_closed_candles("SOL-USDT-SWAP", "1D")))
    assert candle.ts == 1000
    assert len(connect.urls) == 1                       # never dropped and reconnected


def test_defaults_are_sane():
    assert BW.IDLE_TIMEOUT_S >= 30            # klines push every ~250ms
    assert OW.IDLE_TIMEOUT_S > OW._IDLE_PING_S * 2   # room for a ping to be answered
