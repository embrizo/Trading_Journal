"""Binance provider: symbol/interval translation, closed-bar filtering, paging.

Network-free — the HTTP layer is stubbed. The live counterpart is
``tests/evals`` style and marked ``live`` at the bottom.
"""
import asyncio
import time

import pytest

from break_signal.data import EXCHANGES, provider, rest, ws
from break_signal.data import binance_rest as BR

DAY = 86_400_000


# ── translation ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("instid,expected", [
    ("SOL-USDT-SWAP", "SOLUSDT"), ("XRP-USDT-SWAP", "XRPUSDT"),
    ("RKLB-USDT-SWAP", "RKLBUSDT"), ("SOL-USDT", "SOLUSDT"),
    ("sol-usdt-swap", "SOLUSDT"), ("SOLUSDT", "SOLUSDT"), ("BTC-USD-SWAP", "BTCUSD"),
])
def test_symbol_translation(instid, expected):
    assert BR.to_symbol(instid) == expected


@pytest.mark.parametrize("tf,expected", [
    ("1D", "1d"), ("4H", "4h"), ("1H", "1h"), ("12H", "12h"), ("1W", "1w"),
    ("15m", "15m"), ("15M", "15m"), ("5m", "5m"),
])
def test_interval_translation(tf, expected):
    assert BR.to_interval(tf) == expected


def test_unknown_timeframe_is_an_error():
    with pytest.raises(ValueError, match="no Binance interval"):
        BR.to_interval("7Y")


# ── provider dispatch ────────────────────────────────────────────────────────
def test_provider_dispatch():
    assert [m.__name__.rsplit(".", 1)[-1] for m in provider("okx")] == ["okx_rest", "okx_ws"]
    assert [m.__name__.rsplit(".", 1)[-1] for m in provider("binance")] == ["binance_rest", "binance_ws"]
    assert rest(None) is provider("okx")[0]                    # default
    assert rest("BINANCE") is BR                               # case-insensitive
    assert hasattr(ws("binance"), "stream_closed_candles")
    for name in EXCHANGES:
        r, w = provider(name)
        assert hasattr(r, "fetch_candles") and hasattr(r, "REST_URL")
        assert hasattr(w, "stream_closed_candles")
    with pytest.raises(ValueError, match="unknown exchange"):
        provider("kraken")


# ── fetching ─────────────────────────────────────────────────────────────────
def _kline(open_ts, o, h, l, c, v):
    return [open_ts, f"{o}", f"{h}", f"{l}", f"{c}", f"{v}", open_ts + DAY - 1,
            "0", 0, "0", "0", "0"]


class FakeSession:
    """Serves klines from a fixed series, honouring limit and endTime like Binance."""

    def __init__(self, series):
        self.series = series
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        rows = self.series
        if params.get("endTime"):
            end = int(params["endTime"])
            rows = [r for r in rows if r[0] <= end]
        rows = rows[-int(params["limit"]):]
        return _Resp(rows)


class _Resp:
    def __init__(self, rows):
        self.rows, self.status = rows, 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.rows

    async def text(self):
        return str(self.rows)


def _series(n, start_ts):
    return [_kline(start_ts + i * DAY, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i) for i in range(n)]


def test_fetch_drops_the_forming_bar():
    now = int(time.time() * 1000)
    start = now - 4 * DAY                    # bar 5 opens now, so its closeTime is in the future
    s = FakeSession(_series(5, start))
    c = asyncio.run(BR.fetch_candles(s, "SOL-USDT-SWAP", "1D", 10))
    assert len(c) == 4                        # 5 klines, newest still forming
    assert int(c.ts[-1]) == start + 3 * DAY
    assert c.close[0] == pytest.approx(100.5) and c.volume[-1] == pytest.approx(13)
    assert s.calls[0]["symbol"] == "SOLUSDT" and s.calls[0]["interval"] == "1d"


def test_fetch_is_oldest_first_and_capped_to_limit():
    now = int(time.time() * 1000)
    s = FakeSession(_series(50, now - 60 * DAY))
    c = asyncio.run(BR.fetch_candles(s, "SOL-USDT-SWAP", "1D", 10))
    assert len(c) == 10
    assert list(c.ts) == sorted(c.ts)
    assert c.close[0] < c.close[-1]           # the fixture rises, so oldest really is first


def test_fetch_pages_backwards_for_more_history():
    now = int(time.time() * 1000)
    series = _series(40, now - 50 * DAY)
    s = FakeSession(series)
    monkey_max = BR._MAX
    try:
        BR._MAX = 10                          # force paging
        c = asyncio.run(BR.fetch_candles(s, "SOL-USDT-SWAP", "1D", 25))
    finally:
        BR._MAX = monkey_max
    assert len(c) == 25 and list(c.ts) == sorted(c.ts)
    assert len(s.calls) > 1 and "endTime" in s.calls[1]


def test_empty_response_gives_empty_candles():
    c = asyncio.run(BR.fetch_candles(FakeSession([]), "SOL-USDT-SWAP", "1D", 10))
    assert len(c) == 0


def test_error_payload_raises():
    class ErrSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            return _ErrResp()

    class _ErrResp(_Resp):
        def __init__(self):
            super().__init__([])
            self.status = 200

        async def json(self):
            return {"code": -1121, "msg": "Invalid symbol."}

    with pytest.raises(BR.BinanceError, match="Invalid symbol"):
        asyncio.run(BR.fetch_candles(ErrSession([]), "NOPE-USDT-SWAP", "1D", 10))


# ── live (opt in with -m live; needs Binance reachable) ──────────────────────
@pytest.mark.live
def test_live_fetch_matches_the_contract():
    import aiohttp

    async def go():
        async with aiohttp.ClientSession() as s:
            return await BR.fetch_candles(s, "SOL-USDT-SWAP", "1D", 30)

    c = asyncio.run(go())
    assert len(c) == 30 and list(c.ts) == sorted(c.ts)
    assert (c.high >= c.low).all() and (c.volume > 0).all()
    assert int(c.ts[-1]) + DAY <= int(time.time() * 1000) + 1000   # last bar really is closed
