"""Phase J2: watcher → signals table, replay → journal, CSV import, alert footer."""
import asyncio
from pathlib import Path

import pytest

from break_signal.backtest.replay import replay_signals, write_to_journal
from break_signal.config import Config, Watch
from break_signal.core.params import Params
from break_signal.core.state import State
from break_signal.core.types import Signal
from break_signal.journal import analytics
from break_signal.journal.cli import import_signals_csv
from break_signal.journal.db import JournalDB
from break_signal.journal.footer import alert_footer, history_for_signal
from break_signal.notify.base import format_message
from break_signal.watcher import Watcher

from .helpers import append_bar, descending_resistance

REPO = Path(__file__).resolve().parent.parent


def _sig(**over) -> Signal:
    base = dict(symbol="SOL-USDT-SWAP", exchange="OKX", tf="4H", event="break_up", side="resistance",
                price=231.5, line=229.8, atr_dist=0.35, touches=4, age_bars=51, vol_ratio=1.9,
                rsi=61.3, time="2026-09-20T04:00:00Z", line_id="R:1:2")
    base.update(over)
    return Signal(**base)


def _broken_series():
    c = descending_resistance()
    last = len(c)
    line_val = 100.0 + (-0.2) * last
    return append_bar(c, open_=line_val + 0.2, high=line_val + 5.5, low=line_val - 0.5,
                      close=line_val + 5.0, volume=300.0)


# ── signal_history / footer ──────────────────────────────────────────────────
@pytest.fixture
def db():
    d = JournalDB(":memory:")
    yield d
    d.close()


def _seed_history(db, n_long_4h=4):
    """n LONG 4H trades (3 W / 1 L pattern), one linked to a support-side signal (excluded),
    one SHORT 4H, one LONG 1D."""
    sup = db.insert_signal(_sig(event="break_down", side="support", line_id="S:1:2"), "live")
    outcomes = [120, 90, 115, 125]
    for i in range(n_long_4h):
        t = db.add_trade("SOL-USDT-SWAP", "LONG", tf="4H", entry_price=100, sl_price=90,
                         entry_tags=["Retest"] if i != 1 else ["FOMO", "Retest"], opened_ts=i * 1000)
        db.close_trade(t.id, outcomes[i], closed_ts=i * 1000 + 1)
    x = db.add_trade("SOL-USDT-SWAP", "LONG", tf="4H", entry_price=100, sl_price=90, signal_id=sup,
                     opened_ts=9000)
    db.close_trade(x.id, 80, closed_ts=9001)
    s = db.add_trade("SOL-USDT-SWAP", "SHORT", tf="4H", entry_price=100, sl_price=110, opened_ts=9100)
    db.close_trade(s.id, 80, closed_ts=9101)
    d1 = db.add_trade("SOL-USDT-SWAP", "LONG", tf="1D", entry_price=100, sl_price=90, opened_ts=9200)
    db.close_trade(d1.id, 130, closed_ts=9201)


def test_signal_history_matching_rules(db):
    _seed_history(db)
    h = history_for_signal(db, _sig())
    assert h["n"] == 4 and h["wins"] == 3 and h["losses"] == 1          # the support-linked LONG is excluded
    assert h["direction"] == "LONG" and h["tf"] == "4H"
    assert h["win_rate"] == pytest.approx(0.75)
    assert h["best_tag"]["tag"] == "Retest" and h["best_tag"]["n"] == 4
    assert h["worst_tag"] is None                                        # FOMO has n=1 < min_tag_n
    assert sorted(h["trade_ids"]) == [1, 2, 3, 4]
    # a support break maps to SHORT: only the SHORT 4H trade
    assert history_for_signal(db, _sig(event="break_down", side="support"))["n"] == 1
    # 1D
    assert analytics.signal_history(db.list_trades(), "1d", "break_up", "resistance")["n"] == 1
    # symbol narrowing
    assert history_for_signal(db, _sig(symbol="BTC-USDT-SWAP"), symbol_only=True)["n"] == 0


def test_footer_threshold_and_content(db):
    sid = db.insert_signal(_sig(), "live")
    f = alert_footer(db, _sig(), sid)
    assert "0 closed trade(s)" in f and "stats shown from 3" in f
    assert f'journal add "SOL-USDT-SWAP 4H long <entry> sl <sl> tp <tp>" --signal {sid}' in f
    assert f"skip {sid}" in f
    _seed_history(db)
    f = alert_footer(db, _sig(), sid)
    assert "Your history on 4H resistance breaks (LONG): 4 trades · 75% win" in f
    assert "Best tag: Retest" in f
    assert "Worst tag" not in f
    # no signal id → no --signal / skip hint
    assert "--signal" not in alert_footer(db, _sig(), None)


def test_format_message_footer_optional():
    s = _sig()
    plain = format_message(s)
    assert plain == format_message(s, None) == format_message(s, "")
    with_footer = format_message(s, "📒 hello")
    assert with_footer.startswith(plain) and with_footer.endswith("\n\n📒 hello")


# ── watcher persists signals and passes the footer ───────────────────────────
class _CaptureNotifier:
    name = "capture"

    def __init__(self):
        self.sent = []

    async def send(self, text, image=None):
        self.sent.append(text)


def _cfg(**over):
    base = dict(watches=[Watch(symbol="SOL-USDT-SWAP", timeframe="1D")], render_chart=False,
                state_db=":memory:", backfill=100)
    base.update(over)
    return Config(**base)


def test_watcher_inserts_signal_and_footers_alert(db):
    cfg = _cfg()
    state = State(":memory:")
    cap = _CaptureNotifier()
    w = Watcher(cfg, cfg.watches[0], state, [cap], journal=db)
    w.candles = _broken_series()
    asyncio.run(w._on_close())
    sigs = db.list_signals()
    assert len(sigs) == 1
    s = sigs[0]
    assert s.source == "live" and s.event == "break_up" and s.tf == "1D"
    assert s.candle_ts == int(w.candles.ts[-1])
    assert s.line_id.startswith("R:")
    assert len(cap.sent) == 1
    assert "📒" in cap.sent[0] and f"--signal {s.id}" in cap.sent[0]
    # a second close of the same bar is a no-op (dedupe via state)
    asyncio.run(w._on_close())
    assert len(db.list_signals()) == 1 and len(cap.sent) == 1


def test_watcher_without_journal_and_footer_off(db):
    state = State(":memory:")
    cap = _CaptureNotifier()
    w = Watcher(_cfg(), _cfg().watches[0], state, [cap], journal=None)
    w.candles = _broken_series()
    asyncio.run(w._on_close())
    assert len(cap.sent) == 1 and "📒" not in cap.sent[0]

    cfg = _cfg(journal={"history_footer": False})
    state2 = State(":memory:")
    cap2 = _CaptureNotifier()
    w2 = Watcher(cfg, cfg.watches[0], state2, [cap2], journal=db)
    w2.candles = _broken_series()
    asyncio.run(w2._on_close())
    assert len(db.list_signals()) == 1               # still persisted
    assert "📒" not in cap2.sent[0]                  # but no footer


def test_backfill_retries_until_data(monkeypatch):
    import aiohttp
    from break_signal import watcher as W

    calls = {"n": 0}
    sleeps = []

    async def flaky_fetch(session, symbol, tf, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise aiohttp.ClientConnectionError("dns down")
        if calls["n"] == 2:
            return descending_resistance(n=0, pivots=())          # empty → treated as failure
        return _broken_series()

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(W.asyncio, "sleep", fake_sleep)
    w = Watcher(_cfg(), _cfg().watches[0], State(":memory:"), [])
    monkeypatch.setattr(w.rest, "fetch_candles", flaky_fetch)   # whichever exchange is configured
    candles = asyncio.run(w._backfill())
    assert len(candles) > 0 and calls["n"] == 3
    assert sleeps == [W.BACKFILL_RETRY_BASE, W.BACKFILL_RETRY_BASE * 2]   # exponential backoff


# ── replay → journal and CSV import ──────────────────────────────────────────
def test_replay_signals_to_journal(tmp_path):
    c = _broken_series()
    sigs = replay_signals(c, Params(), "1D", "SOL-USDT-SWAP", warmup=60)
    assert len(sigs) >= 1 and all(s.line_id for s in sigs)
    dbp = tmp_path / "j.db"
    inserted, total = write_to_journal(str(dbp), sigs)
    assert inserted == total == len(sigs)
    again, _ = write_to_journal(str(dbp), sigs)
    assert again == 0                                # idempotent
    d = JournalDB(dbp)
    assert {s.source for s in d.list_signals()} == {"backtest"}
    d.close()


def test_import_repo_csv_batch(db):
    csv_path = REPO / "signals_sol_1d_binance.csv"
    inserted, n = import_signals_csv(db, str(csv_path), symbol="SOL-USDT-SWAP")
    assert n == 10 and inserted == 10
    sigs = db.list_signals(limit=100)
    assert all(s.symbol == "SOL-USDT-SWAP" and s.source == "backtest" and s.tf == "1D" for s in sigs)
    assert all(s.line_id.startswith("csv:") for s in sigs)
    newest = sigs[0]
    assert newest.candle_ts > sigs[-1].candle_ts     # newest first
    # re-import is a no-op
    assert import_signals_csv(db, str(csv_path), symbol="SOL-USDT-SWAP")[0] == 0
    # a trade can now link to a backtest signal and skip it
    t = db.skip_signal(newest.id, "not at desk")
    assert t.signal.source == "backtest"
