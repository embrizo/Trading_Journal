"""Live price feed for the dashboard watchlist + one-shot price alerts.

One background task polls OKX tickers for every tracked symbol (the watchlist,
the configured breakout ``watches``, and any symbol with an active alert),
caches ``last`` price + 24h change for the dashboard to read, and fires one-shot
price alerts to the notifiers the moment a target is reached.

Prices come straight from OKX public tickers — symbols are OKX instIds either
way, and one REST call returns every instrument, so the cost is flat regardless
of how many symbols are tracked. This is independent of ``cfg.exchange`` (which
selects the breakout engine's candle source); the watchlist is OKX-priced.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from ..data import okx_rest

if TYPE_CHECKING:
    from ..config import Config
    from ..notify.base import Notifier
    from .db import JournalDB

log = logging.getLogger(__name__)


class PriceFeed:
    def __init__(self, cfg: "Config", db: "JournalDB", notifiers: list["Notifier"]):
        self.cfg = cfg
        self.db = db
        self.notifiers = notifiers
        self.prices: dict[str, dict] = {}   # instId -> {"last":.., "change24h":..}
        self.updated_ts: int | None = None

    def tracked_symbols(self) -> list[str]:
        syms: set[str] = set(self.db.watchlist_all())
        syms.update(w.symbol for w in self.cfg.watches)
        syms.update(a["symbol"] for a in self.db.alerts_active())
        return sorted(syms)

    async def run(self) -> None:
        interval = max(5, int(getattr(self.cfg.web, "watchlist_poll_seconds", 15)))
        log.info("price feed: polling OKX tickers every %ds", interval)
        while True:
            try:
                await self._poll_once()
            except Exception:  # noqa: BLE001 — a poll failure must never kill the loop
                log.exception("price feed: poll failed")
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        async with aiohttp.ClientSession() as session:
            tickers = await okx_rest.fetch_tickers(session)
        tracked = set(self.tracked_symbols())
        # keep only what we track, but preserve the last known price for a symbol
        # momentarily missing from a response rather than blanking the dashboard
        self.prices = {s: tickers[s] for s in tracked if s in tickers}
        self.updated_ts = int(time.time() * 1000)
        await self._check_alerts()

    async def _check_alerts(self) -> None:
        for a in self.db.alerts_active():
            px = self.prices.get(a["symbol"])
            if not px or px.get("last") is None:
                continue
            last = float(px["last"])
            level = float(a["price"])
            hit = last >= level if a["op"] == ">=" else last <= level
            if hit:
                await self._fire(a, last)

    async def _fire(self, a: dict, last: float) -> None:
        # Mark triggered first (one-shot): a notifier failure must not cause a
        # re-fire on the next poll, and the dashboard shows it as done.
        self.db.alert_mark_triggered(a["id"], self.updated_ts or int(time.time() * 1000))
        note = f"\n{a['note']}" if a.get("note") else ""
        text = (f"\U0001F514 Price alert #{a['id']}\n"
                f"{a['symbol']} {a['op']} {a['price']:g}  (now {last:g}){note}")
        log.info("price alert #%s fired: %s %s %g (now %g)",
                 a["id"], a["symbol"], a["op"], float(a["price"]), last)
        if not self.notifiers:
            return
        results = await asyncio.gather(*(n.send(text) for n in self.notifiers),
                                       return_exceptions=True)
        for n, r in zip(self.notifiers, results):
            if isinstance(r, Exception):
                log.error("notifier %s failed on price alert: %s", n.name, r)
