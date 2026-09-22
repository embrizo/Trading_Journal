"""One aiohttp server for the journal's HTTP surface.

- ``/``                 dashboard (Lightweight Charts): candles, live trendlines,
                        alert and trade markers, stats, memories
- ``/api/...``          JSON the dashboard reads (also handy for scripts)
- ``/api/do``           write commands from the page — only when
                        ``web.write_token`` is set, and only with that token
- ``/pine/<secret>``    TradingView webhook (see ``webhook.py``)
- ``/health``

Reads have NO auth — the dashboard is meant for the LAN. Do not port-forward it;
only the webhook path is safe to expose, and only because of its secret. Writes
are refused unless ``web.write_token`` is configured and sent as
``X-Journal-Token``, so the default install stays read-only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import analytics, memory
from .db import JournalDB
from .tools import Tools, json_safe, trade_dict

if TYPE_CHECKING:
    from ..config import Config

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def build_app(cfg: "Config", db: JournalDB, notifiers: list[Any], tools: Tools | None = None):
    from aiohttp import web

    tools = tools or Tools(db, cfg)
    app = web.Application()

    async def health(_req):
        return web.json_response({"ok": True,
                                  "signals": db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
                                  "trades": db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]})

    app.router.add_get("/health", health)

    if cfg.webhook.enabled and cfg.webhook.secret:
        from .webhook import make_pine_handler
        app.router.add_post("/pine/{secret}", make_pine_handler(cfg, db, notifiers))

    if cfg.web.enabled and cfg.web.dashboard:
        _add_dashboard(app, cfg, db, tools)
    return app


def _add_dashboard(app, cfg: "Config", db: JournalDB, tools: Tools) -> None:
    from aiohttp import web

    def _int(req, name, default):
        try:
            return int(req.query.get(name, default))
        except ValueError:
            return default

    def respond(data, status: int = 200):
        # Every payload goes through json_safe: Python's json module would happily
        # emit `Infinity`/`NaN`, which browsers refuse to parse.
        return web.json_response(json_safe(data), status=status)

    async def index(_req):
        # The page is small and changes with every release, so let the browser
        # revalidate instead of caching it: otherwise an updated dashboard keeps
        # serving the old UI until someone thinks to hard-refresh. FileResponse
        # still sets Last-Modified/ETag, so an unchanged page costs a 304.
        return web.FileResponse(STATIC / "dashboard.html",
                                headers={"Cache-Control": "no-cache"})

    async def api_config(_req):
        return respond({
            "watches": [{"symbol": w.symbol, "tf": w.timeframe} for w in cfg.watches],
            "aliases": cfg.journal.symbol_aliases,
            "writes": bool(cfg.web.write_token),   # the page hides its command bar without this
            "help": HELP,
        })

    async def api_summary(_req):
        trades = db.list_trades()
        since30 = analytics.period_to_since("30d")
        return respond({
            "all_time": analytics.summarize(trades),
            "last_30d": analytics.summarize([t for t in trades if (t.opened_ts or 0) >= (since30 or 0)]),
            "tag_stats_entry": analytics.tag_stats(trades, "ENTRY"),
            "by_tf": analytics.feature_stats(trades)["tf"],
            "open_trades": [trade_dict(t) for t in trades if t.status == "OPEN"],
            "equity_curve": analytics.equity_curve(trades),
            "memories": memory.list_memories(db),
        })

    async def api_trades(req):
        return respond(tools.search_trades(
            symbol=req.query.get("symbol") or None, tf=req.query.get("tf") or None,
            status=req.query.get("status") or None, limit=_int(req, "limit", 50)))

    async def api_signals(req):
        return respond(tools.recent_signals(
            req.query.get("symbol") or None, req.query.get("tf") or None,
            req.query.get("source") or None, _int(req, "limit", 50)))

    async def api_chart(req):
        """Candles + engine lines + markers for one (symbol, tf). Needs the exchange."""
        symbol = tools._sym(req.query.get("symbol", "SOL"))
        tf = req.query.get("tf", "1D")
        bars = min(_int(req, "bars", 300), 1000)
        try:
            import aiohttp
            from ..data import rest as data_rest
            async with aiohttp.ClientSession() as session:
                candles = await data_rest(cfg.exchange).fetch_candles(session, symbol, tf, bars)
        except Exception as e:  # noqa: BLE001
            return respond({"symbol": symbol, "tf": tf,
                            "error": f"{cfg.exchange.upper()} fetch failed: {e.__class__.__name__}: {e}"},
                           status=502)
        from .tools import snapshot_from_candles
        snap = snapshot_from_candles(symbol, tf, candles, tools.params)
        ohlc = [{"time": int(candles.ts[i]) // 1000, "open": float(candles.open[i]), "high": float(candles.high[i]),
                 "low": float(candles.low[i]), "close": float(candles.close[i]), "volume": float(candles.volume[i])}
                for i in range(len(candles))]
        first_ts = int(candles.ts[0])
        signals = [s.to_dict() for s in db.list_signals(symbol=symbol, tf=tf, since=first_ts, limit=500)]
        trades = [trade_dict(t) for t in db.list_trades(symbol=symbol, tf=tf, since=first_ts)]
        return respond({"symbol": symbol, "tf": tf, "candles": ohlc, "snapshot": snap,
                        "signals": signals, "trades": trades})

    app.router.add_get("/", index)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/summary", api_summary)
    app.router.add_get("/api/trades", api_trades)
    app.router.add_get("/api/signals", api_signals)
    app.router.add_get("/api/chart", api_chart)
    if cfg.web.write_token:
        app.router.add_post("/api/do", _make_write_handler(cfg, tools, respond))
        log.info("web: writes enabled at /api/do (token required)")


HELP = """add <SYM> [<tf>] long|short <entry> [sl x] [tp x] [size x] [risk x[%]] [#tags] [-- reason]
close <id> <exit> [win|loss|be] [#tags] [reason]
skip <signal_id> [reason]
event <id> <type> [k=v ...]        type: sl_moved | tp_moved | partial_close | added | note
sl <id> <new_price>                logs an sl_moved event; never overwrites the initial stop
tag <id> [exit] #tag [#tag ...]
note <id> <text>"""


def _make_write_handler(cfg: "Config", tools: Tools, respond):
    """POST /api/do {"cmd": "..."} — the CLI's one-line syntax from the browser.

    Everything goes through ``Tools``, so rule checks, auto-linking and context
    copying behave exactly as they do in the CLI, the bot and the MCP server.
    """
    import hmac

    from aiohttp import web

    from .parser import ParseError

    token = cfg.web.write_token

    def _id(word: str) -> int:
        return int(word.lstrip("#"))

    def run(cmd: str) -> dict:
        verb, _, rest = cmd.strip().partition(" ")
        verb, rest = verb.lower(), rest.strip()
        if verb in ("help", "?", ""):
            return {"message": HELP}
        if verb == "add":
            out = tools.add_trade_line(rest)
            t = out["trade"]
            msg = f"logged #{t['id']} {t['symbol']} {t['direction']} @ {t['entry_price']}"
            if out.get("auto_linked_signal"):
                msg += f" (linked to alert #{out['auto_linked_signal']})"
            return {"message": msg, **out}
        if verb == "close":
            out = tools.close_trade_line(rest)
            t = out["trade"]
            msg = f"closed #{t['id']} {t['outcome'] or '-'} R={t['r_multiple'] if t['r_multiple'] is not None else '-'}"
            if out.get("outcome_note"):
                msg += f" — {out['outcome_note']}"
            return {"message": msg, **out}
        if verb == "skip":
            sid, _, reason = rest.partition(" ")
            out = tools.skip_signal(_id(sid), reason=reason.strip() or None)
            return {"message": f"recorded a pass on alert #{_id(sid)}", **out}
        if verb == "event":
            tid, _, tail = rest.partition(" ")
            etype, _, kvs = tail.strip().partition(" ")
            data: dict[str, Any] = {}
            for kv in kvs.split():
                k, _, v = kv.partition("=")
                try:
                    data[k] = float(v)
                except ValueError:
                    data[k] = v
            out = tools.add_event(_id(tid), etype, data)
            return {"message": f"event {etype} on #{_id(tid)}", **out}
        if verb == "sl":
            tid, _, price = rest.partition(" ")
            trade = tools.db.get_trade(_id(tid))
            if trade is None:
                raise KeyError(f"no trade #{_id(tid)}")
            # `from` is where the stop last was: the newest sl_moved, else the initial stop.
            moves = [e for e in trade.events if e.type == "sl_moved"]
            prev = moves[-1].data.get("to") if moves else trade.sl_price
            data = {"to": float(price)}
            if prev is not None:
                data["from"] = prev
            out = tools.add_event(trade.id, "sl_moved", data)
            return {"message": f"stop on #{trade.id} moved to {float(price)} "
                               f"(initial stop {trade.sl_price} kept — R uses initial risk)", **out}
        if verb == "tag":
            tid, _, tail = rest.partition(" ")
            words = tail.split()
            phase = "EXIT" if words and words[0].lower() == "exit" else "ENTRY"
            tags = [w.lstrip("#").replace("_", " ") for w in words if w.startswith("#")]
            if not tags:
                raise ValueError("no #tags given")
            out = tools.tag_trade(_id(tid), tags, phase)
            return {"message": f"tagged #{_id(tid)} {phase.lower()}: {', '.join(tags)}", **out}
        if verb == "note":
            tid, _, text = rest.partition(" ")
            if not text.strip():
                raise ValueError("no note text")
            out = tools.update_trade(_id(tid), notes=text.strip())
            return {"message": f"note saved on #{_id(tid)}", **out}
        raise ValueError(f"unknown command {verb!r}")

    async def do(req):
        sent = req.headers.get("X-Journal-Token", "")
        if not hmac.compare_digest(sent, token):
            log.warning("web: /api/do rejected (bad token) from %s", req.remote)
            return respond({"error": "forbidden — wrong or missing X-Journal-Token"}, status=403)
        body = await req.text()
        if len(body) > 8_000:
            return respond({"error": "body too large"}, status=413)
        try:
            cmd = (json.loads(body or "{}") or {}).get("cmd", "")
        except json.JSONDecodeError:
            return respond({"error": "body must be JSON {\"cmd\": \"...\"}"}, status=400)
        if not isinstance(cmd, str):
            return respond({"error": "cmd must be a string"}, status=400)
        try:
            out = run(cmd)
        except (ParseError, ValueError, KeyError, TypeError, sqlite3.IntegrityError) as e:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else f"{e}"
            log.info("web: /api/do rejected %r — %s", cmd[:80], msg)
            return respond({"error": str(msg), "help": HELP}, status=400)
        log.info("web: /api/do %r", cmd[:80])
        return respond({"ok": True, **out})

    return do


async def serve(cfg: "Config", db: JournalDB, notifiers: list[Any], tools: Tools | None = None) -> None:
    """Background task: run the HTTP server until cancelled."""
    from aiohttp import web

    if cfg.webhook.enabled and not cfg.webhook.secret:
        log.error("webhook.enabled but webhook.secret is empty — webhook route NOT registered")
    runner = web.AppRunner(build_app(cfg, db, notifiers, tools))
    await runner.setup()
    site = web.TCPSite(runner, cfg.web.host, cfg.web.port)
    await site.start()
    log.info("web: http://%s:%d/  (dashboard %s, webhook %s)", cfg.web.host, cfg.web.port,
             "on" if cfg.web.enabled and cfg.web.dashboard else "off",
             "on" if cfg.webhook.enabled and cfg.webhook.secret else "off")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
