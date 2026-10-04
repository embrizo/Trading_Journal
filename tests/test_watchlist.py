"""Watchlist + one-shot price alerts: DB methods and the PriceFeed firing logic."""
import asyncio
from types import SimpleNamespace

import pytest

from break_signal.journal.db import JournalDB
from break_signal.journal.pricefeed import PriceFeed


@pytest.fixture
def db():
    d = JournalDB(":memory:")
    yield d
    d.close()


class FakeNotifier:
    name = "fake"

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, image=None):
        self.sent.append(text)


def _cfg(watches=()):
    watch_objs = [SimpleNamespace(symbol=s, timeframe="1D") for s in watches]
    return SimpleNamespace(watches=watch_objs, web=SimpleNamespace(watchlist_poll_seconds=15))


# ── watchlist ────────────────────────────────────────────────────────────────
def test_watchlist_add_remove_dedup(db):
    db.watchlist_add("SOL-USDT-SWAP")
    db.watchlist_add("SOL-USDT-SWAP")  # idempotent
    db.watchlist_add("BTC-USDT-SWAP")
    assert db.watchlist_all() == ["SOL-USDT-SWAP", "BTC-USDT-SWAP"]  # insertion order
    db.watchlist_remove("SOL-USDT-SWAP")
    assert db.watchlist_all() == ["BTC-USDT-SWAP"]


# ── alerts CRUD ──────────────────────────────────────────────────────────────
def test_alert_add_validates_op(db):
    with pytest.raises(ValueError):
        db.alert_add("SOL-USDT-SWAP", ">", 100.0)


def test_alert_lifecycle(db):
    aid = db.alert_add("SOL-USDT-SWAP", ">=", 250.0, note="breakout target")
    assert [a["id"] for a in db.alerts_active()] == [aid]
    db.alert_mark_triggered(aid, 123)
    assert db.alerts_active() == []
    row = db.alerts_all()[0]
    assert row["active"] == 0 and row["triggered_ts"] == 123
    # re-arming clears the previous fire
    db.alert_set_active(aid, True)
    row = db.alerts_all()[0]
    assert row["active"] == 1 and row["triggered_ts"] is None
    db.alert_remove(aid)
    assert db.alerts_all() == []


# ── PriceFeed one-shot firing ────────────────────────────────────────────────
def test_tracked_symbols_union(db):
    db.watchlist_add("SOL-USDT-SWAP")
    db.alert_add("XRP-USDT-SWAP", "<=", 1.0)
    feed = PriceFeed(_cfg(watches=["BTC-USDT-SWAP"]), db, [])
    assert feed.tracked_symbols() == ["BTC-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"]


def test_alert_fires_once_when_reached(db):
    aid = db.alert_add("SOL-USDT-SWAP", ">=", 250.0, note="to the moon")
    note = FakeNotifier()
    feed = PriceFeed(_cfg(), db, [note])
    feed.updated_ts = 1000

    feed.prices = {"SOL-USDT-SWAP": {"last": 251.0, "change24h": 3.0}}
    asyncio.run(feed._check_alerts())
    assert len(note.sent) == 1
    assert "SOL-USDT-SWAP" in note.sent[0] and "251" in note.sent[0]
    assert db.alerts_active() == []  # one-shot: now inactive

    # a second poll above the level must NOT re-fire
    asyncio.run(feed._check_alerts())
    assert len(note.sent) == 1


def test_alert_does_not_fire_below_level(db):
    db.alert_add("SOL-USDT-SWAP", ">=", 250.0)
    note = FakeNotifier()
    feed = PriceFeed(_cfg(), db, [note])
    feed.prices = {"SOL-USDT-SWAP": {"last": 240.0, "change24h": -1.0}}
    asyncio.run(feed._check_alerts())
    assert note.sent == []
    assert len(db.alerts_active()) == 1  # still armed


def test_alert_le_direction(db):
    db.alert_add("XRP-USDT-SWAP", "<=", 1.00)
    note = FakeNotifier()
    feed = PriceFeed(_cfg(), db, [note])
    feed.prices = {"XRP-USDT-SWAP": {"last": 0.98, "change24h": -2.0}}
    asyncio.run(feed._check_alerts())
    assert len(note.sent) == 1 and db.alerts_active() == []
