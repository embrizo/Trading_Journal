"""R1 wiring tests: the watcher arms a pending on a break and fires the retest,
and the backtest replay reconstructs the same break→retest sequence.

The watcher is built with ``__new__`` so no Config/provider/stream is needed —
only the attributes ``_on_close`` / ``_dispatch`` / ``_arm_retest`` touch are set,
with a real ``State`` (temp db) and a fake notifier capturing the alert text.
"""
from __future__ import annotations

import asyncio

from break_signal.backtest.replay import replay_signals
from break_signal.core.engine import Engine
from break_signal.core.params import Params
from break_signal.core.state import State
from break_signal.watcher import Watcher

from .helpers import MS_PER_DAY, append_bar, descending_resistance

SYM = "SOL-USDT-SWAP"
TF = "1D"
TF_S = 86_400


class FakeNotifier:
    name = "fake"

    def __init__(self):
        self.texts: list[str] = []

    async def send(self, text, image=None):
        self.texts.append(text)


class _Cfg:
    render_chart = False
    chart_bars = 200


def _break_bar(c):
    """A decisive bullish candle that closes above the descending line."""
    last = len(c)
    line_val = 100.0 + (-0.2) * last
    return append_bar(c, open_=line_val + 0.2, high=line_val + 5.5,
                      low=line_val - 0.5, close=line_val + 5.0, volume=300.0)


def _make_watcher(state, params):
    w = Watcher.__new__(Watcher)
    w.cfg = _Cfg()
    w.symbol, w.tf = SYM, TF
    w.state = state
    nf = FakeNotifier()
    w.notifiers = [nf]
    w.journal = None
    w.engine = Engine(params, TF_S, SYM, "OKX", TF)
    w.candles = None
    return w, nf


def test_watcher_arms_pending_on_break_then_fires_retest(tmp_path):
    state = State(str(tmp_path / "s.db"))
    w, nf = _make_watcher(state, Params(use_retest=True))

    # ── bar 1: the break ──────────────────────────────────────────────
    w.candles = _break_bar(descending_resistance())
    asyncio.run(w._on_close())
    assert any("RESISTANCE BREAK" in t for t in nf.texts)
    assert not any("RETEST" in t for t in nf.texts)
    pend = state.pending_retests(SYM, TF)
    assert len(pend) == 1                      # a retest watch was armed
    p = pend[0]
    assert p.side == "R"

    # ── bar 2: pull back to the (frozen) line and hold → the retest ────
    nf.texts.clear()
    break_ts = int(w.candles.ts[-1])
    lv = p.value_at(break_ts + MS_PER_DAY, TF_S)
    w.candles = append_bar(w.candles, open_=lv + 0.1, high=lv + 1.0,
                           low=lv, close=lv + 0.3, volume=120.0)
    asyncio.run(w._on_close())
    assert len(nf.texts) == 1
    assert "RETEST HOLD" in nf.texts[0]
    assert state.pending_retests(SYM, TF) == []    # pending retired once resolved
    state.close()


def test_watcher_retires_pending_on_invalidation(tmp_path):
    state = State(str(tmp_path / "s.db"))
    w, nf = _make_watcher(state, Params(use_retest=True))
    w.candles = _break_bar(descending_resistance())
    asyncio.run(w._on_close())
    p = state.pending_retests(SYM, TF)[0]

    nf.texts.clear()
    break_ts = int(w.candles.ts[-1])
    lv = p.value_at(break_ts + MS_PER_DAY, TF_S)
    # next bar closes decisively back under the line → invalidation, no signal
    w.candles = append_bar(w.candles, open_=lv - 1, high=lv - 0.5, low=lv - 8,
                           close=lv - 6, volume=120.0)
    asyncio.run(w._on_close())
    assert nf.texts == []                              # nothing dispatched
    assert state.pending_retests(SYM, TF) == []        # pending retired (invalidated)
    state.close()


def test_watcher_retires_pending_on_expiry(tmp_path):
    state = State(str(tmp_path / "s.db"))
    w, nf = _make_watcher(state, Params(use_retest=True))
    w.candles = _break_bar(descending_resistance())
    asyncio.run(w._on_close())
    assert len(state.pending_retests(SYM, TF)) == 1

    nf.texts.clear()
    # a bar 13 days on → age 13 > retest_window(12); sits below the line so it
    # breaks nothing new. The pending expires with no signal.
    w.candles = append_bar(w.candles, open_=85.0, high=85.5, low=84.0, close=85.0,
                           volume=120.0, dt_ms=13 * MS_PER_DAY)
    asyncio.run(w._on_close())
    assert nf.texts == []
    assert state.pending_retests(SYM, TF) == []        # pending retired (expired)
    state.close()


def test_watcher_off_by_default_arms_nothing(tmp_path):
    state = State(str(tmp_path / "s.db"))
    w, nf = _make_watcher(state, Params())        # use_retest defaults False
    w.candles = _break_bar(descending_resistance())
    asyncio.run(w._on_close())
    assert any("RESISTANCE BREAK" in t for t in nf.texts)
    assert state.pending_retests(SYM, TF) == []   # never armed when off
    state.close()


def _break_then_retest_series():
    c = _break_bar(descending_resistance())        # break bar at index len-1
    lv = 100.0 - 0.2 * len(c)                       # the line's value at the next bar
    return append_bar(c, open_=lv + 0.1, high=lv + 1.0, low=lv, close=lv + 0.3, volume=120.0)


def test_replay_emits_break_then_retest_when_enabled():
    c = _break_then_retest_series()
    on = replay_signals(c, Params(use_retest=True), TF, SYM, warmup=60)
    assert [s.event for s in on] == ["break_up", "retest_up"]
    # same line id on both legs — the retest is the break's second act
    assert len({s.line_id for s in on}) == 1


def test_replay_unchanged_when_disabled():
    c = _break_then_retest_series()
    off = replay_signals(c, Params(), TF, SYM, warmup=60)
    assert [s.event for s in off] == ["break_up"]
