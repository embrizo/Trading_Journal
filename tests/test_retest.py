"""Synthetic-candle tests for break-and-retest detection (R0).

``check_retests`` resolves a frozen ``PendingRetest`` against one confirmed bar,
so each test builds a pending line and a single candle that either retests it,
invalidates it, or lets it expire. Geometry is recomputed from the frozen
``(ts_ref, value_ref, slope)`` alone — the candle series carries no pivots, which
is the point: a broken line survives its anchor pivots aging out of the window.
"""
from __future__ import annotations

import numpy as np
import pytest

from break_signal.core.engine import Engine
from break_signal.core.params import Params
from break_signal.core.retest import PendingRetest, check_retests
from break_signal.core.state import State
from break_signal.core.types import Candles

TF_S = 86_400
MS = 86_400_000


def _candles(rows):
    """rows: list of (age_days, open, high, low, close); ts = age_days × 1 day."""
    return Candles(
        ts=np.array([r[0] * MS for r in rows], dtype=np.int64),
        open=np.array([r[1] for r in rows], dtype=float),
        high=np.array([r[2] for r in rows], dtype=float),
        low=np.array([r[3] for r in rows], dtype=float),
        close=np.array([r[4] for r in rows], dtype=float),
        volume=np.zeros(len(rows)),
    )


def _pending(side="R", value=100.0, slope=0.0, ts_ref=0, broke_ts=0):
    return PendingRetest(
        line_id=f"{side}:{ts_ref}:{ts_ref + MS}", side=side, ts_ref=ts_ref,
        value_ref=value, slope=slope, touches=4, broke_ts=broke_ts,
    )


def _run(rows, pending, params=None, atr=1.0, rsi=55.0):
    c = _candles(rows)
    params = params or Params(use_retest=True)
    return check_retests(c, [pending], atr, rsi, params, len(c) - 1, TF_S,
                         "SOL-USDT-SWAP", "OKX", "1D")


# ── the three core outcomes ──────────────────────────────────────────────
def test_clean_retest_fires_one_up_signal():
    # flat resistance at 100 broke up on day 0; day 3 wicks down to tag it and
    # closes back above → one retest_up.
    res = _run([(0, 100, 100, 100, 100), (3, 100.2, 100.8, 100.1, 100.5)], _pending())
    assert len(res.signals) == 1
    sig = res.signals[0]
    assert sig.event == "retest_up" and sig.side == "resistance"
    assert sig.price == 100.5 and sig.line == pytest.approx(100.0)
    assert sig.touches == 4
    assert sig.age_bars == 3                       # bars since the anchor (ts_a=0)
    assert sig.vol_ratio == 0.0                    # no volume filter on the retest
    assert res.retire == [(sig.line_id, "retested")]


def test_pullback_through_invalidates_no_signal():
    # day 3 wicks to the line but CLOSES decisively back under it → invalidated.
    p = _pending()
    res = _run([(0, 100, 100, 100, 100), (3, 99.8, 100.0, 99.0, 99.5)], p)
    assert res.signals == []
    assert res.retire == [(p.line_id, "invalidated")]


def test_runaway_expires_no_signal():
    # day 13 is past retest_window (12); price never came back → expired.
    p = _pending()
    res = _run([(0, 100, 100, 100, 100), (13, 105, 107, 105, 106)], p)
    assert res.signals == []
    assert res.retire == [(p.line_id, "expired")]


# ── symmetry, carry-over, boundary ─────────────────────────────────────────
def test_support_break_down_mirror():
    # flat support at 100 broke down; day 3 wicks up to tag it, closes back below.
    p = _pending(side="S")
    res = _run([(0, 100, 100, 100, 100), (3, 99.6, 99.9, 99.0, 99.5)], p)
    assert len(res.signals) == 1
    sig = res.signals[0]
    assert sig.event == "retest_down" and sig.side == "support"
    assert res.retire == [(p.line_id, "retested")]


def test_untagged_bar_carries_over():
    # within the window but price never returns to the line: no signal, no retire
    # (the pending must survive to the next bar).
    res = _run([(0, 100, 100, 100, 100), (3, 103, 104, 103, 104)], _pending())
    assert res.signals == [] and res.retire == []


def test_window_boundary_last_bar_still_eligible():
    # exactly at retest_window (12) a clean retest still fires; one past expires.
    p = _pending()
    on = _run([(0, 100, 100, 100, 100), (12, 100.2, 100.8, 100.1, 100.5)], p)
    assert [s.event for s in on.signals] == ["retest_up"]
    off = _run([(0, 100, 100, 100, 100), (13, 100.2, 100.8, 100.1, 100.5)], p)
    assert off.signals == [] and off.retire == [(p.line_id, "expired")]


def test_frozen_geometry_survives_pivots_aging_out():
    # sloped line (−0.2/bar) frozen at day 0; by day 5 its value is 99.0. The
    # candle series has no pivots at all, yet the line value is recomputed from
    # the frozen geometry alone and the retest fires at exactly 99.0.
    p = _pending(slope=-0.2)
    res = _run([(0, 100, 100, 100, 100), (5, 99.1, 99.5, 99.0, 99.3)], p)
    assert len(res.signals) == 1
    assert res.signals[0].line == pytest.approx(99.0)
    assert res.signals[0].event == "retest_up"


# ── the rejection-body toggle ──────────────────────────────────────────────
def test_reject_body_toggle_filters_weak_hold():
    # tags + holds, but the close sits in the LOWER half of the bar.
    rows = [(0, 100, 100, 100, 100), (3, 100.1, 100.9, 100.1, 100.2)]
    fires = _run(rows, _pending())                                   # default: off
    assert [s.event for s in fires.signals] == ["retest_up"]
    strict = _run(rows, _pending(), params=Params(use_retest=True, retest_reject_body=True))
    assert strict.signals == [] and strict.retire == []              # rejected, carried over


# ── engine gate + retire plumbing ──────────────────────────────────────────
def _flat_series(n=20, level=100.0):
    rows = [(i, level, level + 0.2, level - 0.2, level) for i in range(n)]
    return _candles(rows)


def _engine(params):
    return Engine(params, tf_seconds=TF_S, symbol="SOL-USDT-SWAP",
                  exchange="OKX", tf_label="1D")


def _retest_series():
    """A flat series (atr>0, no breaks) whose last bar retests a pending at 100."""
    c = _flat_series()
    last_ts = int(c.ts[-1])
    return c, last_ts


def test_engine_off_by_default_ignores_pending():
    c, last_ts = _retest_series()
    p = _pending(ts_ref=last_ts - 3 * MS, broke_ts=last_ts - 3 * MS)
    res = _engine(Params()).evaluate(c, pending=[p])     # use_retest defaults False
    assert [s for s in res.signals if s.event.startswith("retest")] == []
    assert res.retire == []


def test_engine_use_retest_wires_check_retests():
    c, last_ts = _retest_series()
    # retest bar: wick exactly to the line, close just above → fires for any atr>0
    c = Candles(
        ts=np.append(c.ts[:-1], last_ts),
        open=np.append(c.open[:-1], 100.0),
        high=np.append(c.high[:-1], 100.3),
        low=np.append(c.low[:-1], 100.0),
        close=np.append(c.close[:-1], 100.1),
        volume=np.append(c.volume[:-1], 0.0),
    )
    p = _pending(ts_ref=last_ts - 3 * MS, broke_ts=last_ts - 3 * MS)
    res = _engine(Params(use_retest=True)).evaluate(c, pending=[p])
    retests = [s for s in res.signals if s.event == "retest_up"]
    assert len(retests) == 1
    assert res.retire == [(p.line_id, "retested")]


# ── state persistence round-trip ───────────────────────────────────────────
def test_state_pending_roundtrip(tmp_path):
    st = State(str(tmp_path / "state.db"))
    p = _pending(ts_ref=5 * MS, broke_ts=5 * MS)
    st.add_pending("SOL-USDT-SWAP", "1D", p)
    st.add_pending("SOL-USDT-SWAP", "1D", p)              # idempotent
    got = st.pending_retests("SOL-USDT-SWAP", "1D")
    assert got == [p]
    assert st.pending_retests("SOL-USDT-SWAP", "4H") == []   # scoped by tf
    st.retire_pending("SOL-USDT-SWAP", "1D", p.line_id)
    assert st.pending_retests("SOL-USDT-SWAP", "1D") == []
    st.close()
