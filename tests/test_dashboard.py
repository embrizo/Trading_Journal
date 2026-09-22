"""J6: the read-only dashboard's JSON API and page."""
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from break_signal.config import Config, Watch
from break_signal.journal import web as WEB
from break_signal.journal.db import JournalDB
from break_signal.journal.tools import Tools

from .helpers import append_bar, descending_resistance


@pytest.fixture
def setup():
    cfg = Config(watches=[Watch(symbol="SOL-USDT-SWAP", timeframe="1D"), Watch(symbol="SOL-USDT-SWAP", timeframe="4H")],
                 web={"enabled": True})
    db = JournalDB(":memory:")
    tools = Tools(db, cfg)
    a = tools.add_trade("SOL", "LONG", tf="1D", entry_price=100, sl_price=90, tags=["Breakout"], auto_link=False)["trade"]["id"]
    tools.close_trade(a, 120)
    tools.add_trade("SOL", "SHORT", tf="4H", entry_price=100, sl_price=110, auto_link=False)
    db.insert_signal(dict(symbol="SOL-USDT-SWAP", exchange="OKX", tf="1D", event="break_up", side="resistance",
                          price=1, line=1, atr_dist=0.4, touches=3, age_bars=1, vol_ratio=1.5, rsi=60.0,
                          time="1970-01-30T00:00:00Z", line_id="x"), "live")
    yield cfg, db, tools
    db.close()


def _run(coro):
    return asyncio.run(coro)


def test_page_and_api(setup):
    cfg, db, tools = setup
    app = WEB.build_app(cfg, db, [], tools)

    async def go():
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/")
            assert r.status == 200 and "Break Signal" in await r.text() and "lightweight-charts" in await r.text()
            # the page must revalidate, or an updated dashboard keeps serving the old UI
            assert "no-cache" in r.headers.get("Cache-Control", "")
            conf = await (await c.get("/api/config")).json()
            assert conf["watches"] == [{"symbol": "SOL-USDT-SWAP", "tf": "1D"}, {"symbol": "SOL-USDT-SWAP", "tf": "4H"}]
            raw = await (await c.get("/api/summary")).text()
            assert "Infinity" not in raw and "NaN" not in raw          # browsers reject these (PF is inf here)
            s = json.loads(raw)
            assert s["all_time"]["profit_factor"] == "inf"
            assert s["all_time"]["n"] == 1 and s["all_time"]["wins"] == 1
            assert [t["id"] for t in s["open_trades"]] == [2]
            assert s["tag_stats_entry"]["Breakout"]["n"] == 1 and s["equity_curve"][0]["cum_r"] == 2.0
            assert s["memories"] == []
            tr = await (await c.get("/api/trades?limit=1")).json()
            assert tr["n"] == 1 and tr["trades"][0]["id"] == 2
            tr = await (await c.get("/api/trades?status=CLOSED")).json()
            assert [t["id"] for t in tr["trades"]] == [1]
            sg = await (await c.get("/api/signals")).json()
            assert len(sg) == 1 and sg[0]["candle"] == "1970-01-30T00:00:00Z"
            assert (await c.get("/api/trades?limit=abc")).status == 200          # bad int → default
            assert (await c.get("/health")).status == 200
            assert (await c.post("/pine/x", data="{}")).status == 404          # webhook off
    _run(go())


def test_chart_endpoint_with_synthetic_candles(setup, monkeypatch):
    cfg, db, tools = setup
    c0 = descending_resistance()
    last = len(c0)
    line_val = 100.0 + (-0.2) * last
    series = append_bar(c0, open_=line_val + 0.2, high=line_val + 5.5, low=line_val - 0.5, close=line_val + 5.0, volume=300.0)

    async def fake_fetch(session, symbol, tf, limit):
        return series

    from break_signal.data import okx_rest
    monkeypatch.setattr(okx_rest, "fetch_candles", fake_fetch)
    app = WEB.build_app(cfg, db, [], tools)

    async def go():
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/chart?symbol=SOL&tf=1D&bars=300")
            assert r.status == 200
            d = await r.json()
            assert d["symbol"] == "SOL-USDT-SWAP" and len(d["candles"]) == len(series)
            assert set(d["candles"][0]) == {"time", "open", "high", "low", "close", "volume"}
            assert d["candles"][0]["time"] == int(series.ts[0]) // 1000
            ln = d["snapshot"]["lines"][0]
            assert {"anchor_ts", "anchor_value", "last_ts", "value", "side"} <= set(ln)
            assert ln["last_ts"] == int(series.ts[-1]) and ln["anchor_ts"] < ln["last_ts"]
            assert d["snapshot"]["last_bar_signal"]["event"] == "break_up"
            assert [s["id"] for s in d["signals"]] == [1]                     # stored 1D signal inside the window
            assert [t["id"] for t in d["trades"]] == [1]                      # the 1D trade; the 4H one is filtered out
    _run(go())


def test_chart_endpoint_reports_fetch_failure(setup, monkeypatch):
    cfg, db, tools = setup

    async def boom(session, symbol, tf, limit):
        raise OSError("dns down")

    from break_signal.data import okx_rest
    monkeypatch.setattr(okx_rest, "fetch_candles", boom)
    app = WEB.build_app(cfg, db, [], tools)

    async def go():
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/chart?symbol=SOL&tf=1D")
            assert r.status == 502 and "OKX fetch failed" in (await r.json())["error"]
    _run(go())


def test_dashboard_off_when_web_disabled():
    cfg = Config(watches=[Watch(symbol="SOL-USDT-SWAP", timeframe="1D")], web={"enabled": False})
    db = JournalDB(":memory:")
    app = WEB.build_app(cfg, db, [])

    async def go():
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/")).status == 404 and (await c.get("/api/summary")).status == 404
    _run(go())
    db.close()
