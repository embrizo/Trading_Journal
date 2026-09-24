"""M6 hit-rate report: outcome resolution on hand-built candles.

Every case below has an answer you can work out on paper, so a failure means the
evaluator changed, not the data.
"""
import numpy as np
import pytest

from break_signal.backtest import report as R
from break_signal.core.types import Candles, Signal
from break_signal.journal import analytics

DAY = 86_400_000


def _candles(bars):
    """bars: list of (high, low, close). Open/volume are filler."""
    n = len(bars)
    return Candles(
        ts=np.arange(n, dtype=np.int64) * DAY,
        open=np.array([b[2] for b in bars], dtype=float),
        high=np.array([b[0] for b in bars], dtype=float),
        low=np.array([b[1] for b in bars], dtype=float),
        close=np.array([b[2] for b in bars], dtype=float),
        volume=np.full(n, 100.0),
    )


def _iso(ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sig(i, event="break_up", price=110.0, line=100.0, symbol="SOL-USDT-SWAP", tf="1D"):
    """A signal that fired on bar ``i`` of the fixture series."""
    return Signal(symbol=symbol, exchange="OKX", tf=tf, event=event, side="resistance",
                  price=price, line=line, atr_dist=0.4, touches=3, age_bars=5,
                  vol_ratio=1.8, rsi=62.0, time=_iso(i * DAY), line_id=f"R:{i}:{i + 1}")


# ── outcome resolution ───────────────────────────────────────────────────────
def test_long_reaches_target_first():
    # entry 110, line 100 -> risk 10, target at 2R = 130
    bars = [(110, 109, 110)] + [(120, 108, 119), (131, 118, 130)]
    rows = R.evaluate(_candles(bars), [_sig(0)], rr=2.0, horizon=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["exit_kind"] == "target" and r["outcome"] == "WIN"
    assert r["r_multiple"] == pytest.approx(2.0)
    assert r["bars_held"] == 2 and r["ambiguous"] is False


def test_long_falls_back_through_the_line():
    bars = [(110, 109, 110)] + [(115, 108, 112), (113, 99, 101)]
    rows = R.evaluate(_candles(bars), [_sig(0)], rr=2.0, horizon=10)
    assert rows[0]["exit_kind"] == "stop" and rows[0]["outcome"] == "LOSS"
    assert rows[0]["r_multiple"] == pytest.approx(-1.0)


def test_unresolved_is_closed_at_the_horizon():
    bars = [(110, 109, 110)] + [(112, 108, 111)] * 5
    rows = R.evaluate(_candles(bars), [_sig(0)], rr=2.0, horizon=3)
    r = rows[0]
    assert r["exit_kind"] == "horizon" and r["bars_held"] == 3
    assert r["exit"] == pytest.approx(111.0)
    assert r["r_multiple"] == pytest.approx(0.1)          # (111-110)/10

def test_same_bar_target_and_stop_counts_as_a_loss_and_is_flagged():
    bars = [(110, 109, 110)] + [(135, 95, 120)]           # spans both
    rows = R.evaluate(_candles(bars), [_sig(0)], rr=2.0, horizon=10)
    assert rows[0]["ambiguous"] is True
    assert rows[0]["exit_kind"] == "stop" and rows[0]["outcome"] == "LOSS"


def test_short_mirrors_the_long_case():
    # break_down: entry 90, line 100 -> risk 10, target 2R = 70
    bars = [(91, 89, 90)] + [(92, 80, 82), (83, 69, 70)]
    rows = R.evaluate(_candles(bars), [_sig(0, event="break_down", price=90.0, line=100.0)],
                      rr=2.0, horizon=10)
    assert rows[0]["direction"] == "SHORT" and rows[0]["exit_kind"] == "target"
    assert rows[0]["r_multiple"] == pytest.approx(2.0)


def test_signal_on_the_last_bar_is_not_judged():
    bars = [(110, 109, 110)]
    assert R.evaluate(_candles(bars), [_sig(0)], rr=2.0, horizon=10) == []


def test_only_later_candles_are_used():
    """A target touched BEFORE the signal must not count — no look-ahead."""
    bars = [(200, 100, 110), (111, 109, 110), (112, 108, 111)]   # bar 0 already spans 130
    rows = R.evaluate(_candles(bars), [_sig(1)], rr=2.0, horizon=10)
    assert rows[0]["exit_kind"] == "horizon"                    # bar 0 ignored


# ── aggregation goes through analytics ───────────────────────────────────────
def test_summary_matches_analytics_on_the_same_rows():
    bars = ([(110, 109, 110)] + [(131, 118, 130)]                 # win  +2R
            + [(110, 109, 110)] + [(113, 99, 101)])               # loss -1R
    rows = R.evaluate(_candles(bars), [_sig(0), _sig(2)], rr=2.0, horizon=1)
    s = R.summarize(rows)
    assert s["n"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert s["total_r"] == pytest.approx(1.0) and s["avg_r"] == pytest.approx(0.5)
    assert s["profit_factor"] == pytest.approx(2.0)
    # the synthetic trades really are what analytics summarised
    assert analytics.summarize(R.as_trades(rows)) == s


def test_render_is_markdown_with_a_row_per_market_and_a_pooled_line():
    bars = [(110, 109, 110)] + [(131, 118, 130)]
    rows = R.evaluate(_candles(bars), [_sig(0)], rr=2.0, horizon=5)
    text = R.render([("SOL-USDT-SWAP", "1D", rows), ("BTC-USDT-SWAP", "1D", [])], 2.0, 30)
    assert "| SOL-USDT-SWAP 1D | 1 |" in text
    assert "| BTC-USDT-SWAP 1D | 0 |" in text
    assert "**all**" in text and "Pooled expectancy" in text
    assert "no look-ahead" in text
    assert "Not a strategy result" in text and "No costs" in text   # caveats travel with it
    assert "Signals from" in text
