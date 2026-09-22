"""POST /api/do — logging and editing from the dashboard.

The route exists only when ``web.write_token`` is set, requires that token, and
goes through ``Tools`` so rules, auto-link and context copying behave exactly as
they do in the CLI.
"""
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from break_signal.config import Config, Watch
from break_signal.journal import web as WEB
from break_signal.journal.db import JournalDB
from break_signal.journal.tools import Tools

TOKEN = "s3cret-write-token"
HDR = {"X-Journal-Token": TOKEN}


def _cfg(token: str = TOKEN, **web):
    return Config(watches=[Watch(symbol="SOL-USDT-SWAP", timeframe="4H")],
                  web={"enabled": True, "write_token": token, **web},
                  journal={"symbol_aliases": {"SOL": "SOL-USDT-SWAP"}})


@pytest.fixture
def setup():
    cfg = _cfg()
    db = JournalDB(":memory:", account_size=10_000)
    tools = Tools(db, cfg)
    db.insert_signal(dict(symbol="SOL-USDT-SWAP", exchange="OKX", tf="4H", event="break_up", side="resistance",
                          price=231.5, line=229.8, atr_dist=0.4, touches=3, age_bars=1, vol_ratio=1.5, rsi=60.0,
                          time="2026-09-20T04:00:00Z", line_id="x"), "live")
    yield cfg, db, tools
    db.close()


def _run(coro):
    return asyncio.run(coro)


async def _do(c, cmd, headers=HDR):
    return await c.post("/api/do", data=json.dumps({"cmd": cmd}), headers=headers)


def test_full_trade_lifecycle_from_the_page(setup):
    cfg, db, tools = setup
    app = WEB.build_app(cfg, db, [], tools)

    async def go():
        async with TestClient(TestServer(app)) as c:
            assert (await (await c.get("/api/config")).json())["writes"] is True

            r = await _do(c, "add SOL 4H long 231.5 sl 225 tp 245 #breakout #retest -- clean retest")
            body = await r.json()
            assert r.status == 200 and body["ok"] is True
            tid = body["trade"]["id"]
            assert "logged #1" in body["message"]
            assert body["trade"]["entry_tags"] == ["Breakout", "Retest"]
            assert body["trade"]["entry_reason"] == "clean retest"

            # a stop move is an event: the initial stop is untouched, so R keeps its meaning
            r = await _do(c, f"sl {tid} 231.5")
            body = await r.json()
            assert r.status == 200 and "moved to 231.5" in body["message"]
            assert db.get_trade(tid).sl_price == 225
            ev = db.get_trade(tid).events[-1]
            assert ev.type == "sl_moved" and ev.data == {"to": 231.5, "from": 225}
            assert body["rule_violations"] == []          # tightened, not widened

            r = await _do(c, f"note {tid} felt calm, no chasing")
            assert r.status == 200 and "felt calm" in db.get_trade(tid).notes
            r = await _do(c, f"tag {tid} exit #hit_tp")
            assert r.status == 200 and db.get_trade(tid).exit_tags == ["Hit TP"]

            r = await _do(c, f"close {tid} 245 hit TP, held the plan")
            body = await r.json()
            assert r.status == 200 and body["trade"]["outcome"] == "WIN"
            assert body["trade"]["r_multiple"] == pytest.approx(2.0769, abs=1e-3)

            r = await _do(c, "skip 1 not at desk")
            assert r.status == 200 and "#1" in (await r.json())["message"]
            assert [t.status for t in db.list_trades() if t.signal_id == 1] == ["SKIPPED"]
    _run(go())


def test_token_is_required(setup):
    cfg, db, tools = setup
    app = WEB.build_app(cfg, db, [], tools)

    async def go():
        async with TestClient(TestServer(app)) as c:
            for headers in ({}, {"X-Journal-Token": ""}, {"X-Journal-Token": "wrong"}):
                r = await _do(c, "add SOL 4H long 100 sl 90", headers=headers)
                assert r.status == 403 and "forbidden" in (await r.json())["error"]
            assert db.list_trades() == []                 # nothing written
    _run(go())


def test_no_route_at_all_without_a_configured_token():
    cfg = _cfg(token="")
    db = JournalDB(":memory:")
    app = WEB.build_app(cfg, db, [], Tools(db, cfg))

    async def go():
        async with TestClient(TestServer(app)) as c:
            assert (await (await c.get("/api/config")).json())["writes"] is False
            r = await _do(c, "add SOL 4H long 100 sl 90")
            assert r.status == 404                        # read-only install
    _run(go())
    db.close()


def test_bad_commands_are_rejected_with_help(setup):
    cfg, db, tools = setup
    app = WEB.build_app(cfg, db, [], tools)

    async def go():
        async with TestClient(TestServer(app)) as c:
            for cmd in ("frobnicate 1", "add", "close 999 100", "sl 999 1.0", "tag 1", "note 1"):
                r = await _do(c, cmd)
                body = await r.json()
                assert r.status == 400, cmd
                assert body["error"] and "add <SYM>" in body["help"]
            assert db.list_trades() == []
            # help is not an error
            r = await _do(c, "help")
            assert r.status == 200 and "close <id>" in (await r.json())["message"]
            # malformed envelope and oversized bodies
            r = await c.post("/api/do", data="not json", headers=HDR)
            assert r.status == 400
            r = await c.post("/api/do", data=json.dumps({"cmd": "x" * 9000}), headers=HDR)
            assert r.status == 413
    _run(go())


def test_rule_violations_come_back_to_the_page(setup):
    cfg, db, tools = setup
    app = WEB.build_app(cfg, db, [], tools)

    async def go():
        async with TestClient(TestServer(app)) as c:
            r = await _do(c, "add SOL 4H long 100 sl 90 tp 105 risk 500 #fomo")
            body = await r.json()
            names = {v["name"] for v in body["rule_violations"]}
            assert {"Max risk 1%", "No FOMO entries", "Planned R:R >= 1.5"} <= names
            tid = body["trade"]["id"]
            # widening the stop is still caught from here
            r = await _do(c, f"sl {tid} 80")
            assert "Never widen the stop" in {v["name"] for v in (await r.json())["rule_violations"]}
    _run(go())
