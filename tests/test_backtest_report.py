"""M6 hit-rate report: outcome resolution on hand-built candles.

Every case below has an answer you can work out on paper, so a failure means the
evaluator changed, not the data.
"""
import numpy as np
import pytest

from break_signal.backtest import report as R
from break_signal.core.params import Params
from break_signal.core.types import Candles, Signal
from break_signal.journal import analytics
from break_signal.journal.db import iso_to_ms

DAY = 86_400_000


def _candles(bars):
    """bars: (high, low, close) or (high, low, close, open).

    The open matters for gap pricing, so it can be given explicitly; it defaults to
    the close, which means no gap.
    """
    n = len(bars)
    return Candles(
        ts=np.arange(n, dtype=np.int64) * DAY,
        open=np.array([b[3] if len(b) > 3 else b[2] for b in bars], dtype=float),
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


def _eval(bars, signals, rr=2.0, horizon=10):
    """evaluate() returns (judged rows, unresolved count)."""
    return R.evaluate(_candles(bars), signals, rr=rr, horizon=horizon)


# ── outcome resolution ───────────────────────────────────────────────────────
def test_long_reaches_target_first():
    # entry 110, line 100 -> risk 10, target at 2R = 130
    bars = [(110, 109, 110)] + [(120, 108, 119), (131, 118, 130)]
    rows, unresolved = _eval(bars, [_sig(0)])
    assert len(rows) == 1 and unresolved == 0
    r = rows[0]
    assert r["exit_kind"] == "target" and r["outcome"] == "WIN"
    assert r["r_multiple"] == pytest.approx(2.0)
    assert r["bars_held"] == 2 and r["ambiguous"] is False


def test_long_falls_back_through_the_line():
    bars = [(110, 109, 110)] + [(115, 108, 112), (113, 99, 101)]
    rows, _ = _eval(bars, [_sig(0)])
    assert rows[0]["exit_kind"] == "stop" and rows[0]["outcome"] == "LOSS"
    assert rows[0]["r_multiple"] == pytest.approx(-1.0)


def test_unresolved_is_closed_at_the_horizon():
    bars = [(110, 109, 110)] + [(112, 108, 111)] * 5
    rows, unresolved = _eval(bars, [_sig(0)], horizon=3)
    r = rows[0]
    assert unresolved == 0                                # the full horizon was available
    assert r["exit_kind"] == "horizon" and r["bars_held"] == 3
    assert r["exit"] == pytest.approx(111.0)
    assert r["r_multiple"] == pytest.approx(0.1)          # (111-110)/10


def test_a_gap_through_the_stop_costs_more_than_one_r():
    """A stop is a stop-market order. Pricing it exactly would make every loss
    -1.00R however violently the market gapped."""
    # entry 110, line 100 -> risk 10. Next bar OPENS at 85, far below the stop.
    bars = [(110, 109, 110), (86, 80, 82, 85)]             # high, low, close, open
    rows, _ = _eval(bars, [_sig(0)])
    assert rows[0]["exit_kind"] == "stop"
    assert rows[0]["exit"] == pytest.approx(85.0)          # the open, not the stop
    assert rows[0]["r_multiple"] == pytest.approx(-2.5)


def test_a_normal_stop_touch_still_fills_at_the_stop():
    bars = [(110, 109, 110), (112, 99, 101, 111)]          # opens above the stop
    rows, _ = _eval(bars, [_sig(0)])
    assert rows[0]["exit"] == pytest.approx(100.0) and rows[0]["r_multiple"] == pytest.approx(-1.0)


def test_a_favourable_gap_through_the_target_is_not_credited():
    """A take-profit is a limit order: it fills at the limit, never better."""
    bars = [(110, 109, 110), (150, 140, 145, 142)]         # opens above the 130 target
    rows, _ = _eval(bars, [_sig(0)])
    assert rows[0]["exit_kind"] == "target"
    assert rows[0]["exit"] == pytest.approx(130.0) and rows[0]["r_multiple"] == pytest.approx(2.0)


def test_short_gap_through_the_stop_is_mirrored():
    # break_down: entry 90, line 100 -> risk 10; next bar opens at 115, above the stop
    bars = [(91, 89, 90), (120, 114, 118, 115)]
    rows, _ = _eval(bars, [_sig(0, event="break_down", price=90.0, line=100.0)])
    assert rows[0]["exit"] == pytest.approx(115.0)
    assert rows[0]["r_multiple"] == pytest.approx(-2.5)


def test_same_bar_target_and_stop_counts_as_a_loss_and_is_flagged():
    bars = [(110, 109, 110)] + [(135, 95, 120)]           # spans both
    rows, _ = _eval(bars, [_sig(0)])
    assert rows[0]["ambiguous"] is True
    assert rows[0]["exit_kind"] == "stop" and rows[0]["outcome"] == "LOSS"


def test_short_mirrors_the_long_case():
    # break_down: entry 90, line 100 -> risk 10, target 2R = 70
    bars = [(91, 89, 90)] + [(92, 80, 82), (83, 69, 70)]
    rows, _ = _eval(bars, [_sig(0, event="break_down", price=90.0, line=100.0)])
    assert rows[0]["direction"] == "SHORT" and rows[0]["exit_kind"] == "target"
    assert rows[0]["r_multiple"] == pytest.approx(2.0)


def test_signal_on_the_last_bar_is_not_judged():
    rows, unresolved = _eval([(110, 109, 110)], [_sig(0)])
    assert rows == [] and unresolved == 1


# ── right-censoring: too little forward data is not a flat outcome ───────────
def test_signal_without_a_full_horizon_is_excluded_not_counted_flat():
    """Two bars after the signal but a 10-bar horizon: it neither hit nor failed,
    so counting its interim close (~0R) as an outcome would bias expectancy."""
    bars = [(110, 109, 110), (112, 108, 111), (112, 108, 111)]
    rows, unresolved = _eval(bars, [_sig(0)], horizon=10)
    assert rows == [] and unresolved == 1


def test_a_signal_that_resolved_early_is_kept_even_near_the_end():
    """Resolution beats censoring: it hit the target with data to spare."""
    bars = [(110, 109, 110), (131, 118, 130)]
    rows, unresolved = _eval(bars, [_sig(0)], horizon=10)
    assert unresolved == 0 and rows[0]["exit_kind"] == "target"


def test_only_later_candles_are_used():
    """A target touched BEFORE the signal must not count — no look-ahead."""
    bars = [(200, 100, 110), (111, 109, 110), (112, 108, 111)]   # bar 0 already spans 130
    rows, _ = _eval(bars, [_sig(1)], horizon=1)
    assert rows[0]["exit_kind"] == "horizon"                    # bar 0 ignored


# ── aggregation goes through analytics ───────────────────────────────────────
def test_summary_matches_analytics_on_the_same_rows():
    bars = ([(110, 109, 110)] + [(131, 118, 130)]                 # win  +2R
            + [(110, 109, 110)] + [(113, 99, 101)])               # loss -1R
    rows, _ = _eval(bars, [_sig(0), _sig(2)], horizon=1)
    s = R.summarize(rows)
    assert s["n"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert s["total_r"] == pytest.approx(1.0) and s["avg_r"] == pytest.approx(0.5)
    assert s["profit_factor"] == pytest.approx(2.0)
    # the synthetic trades really are what analytics summarised
    assert analytics.summarize(R.as_trades(rows)) == s


def _market(symbol, tf, rows, unresolved=0, window=None):
    m = {"symbol": symbol, "tf": tf, "rows": rows, "unresolved": unresolved}
    if window:
        m["window"] = window
    return m


def test_comparability_uses_the_data_window_not_the_signal_span():
    """Two markets over the same candles are comparable even if their first and
    last signals fall months apart — judging by signal span cried wolf."""
    early, _ = _eval([(110, 109, 110)] + [(131, 118, 130)], [_sig(0)], horizon=5)
    late, _ = _eval([(110, 109, 110)] * 40 + [(131, 118, 130)], [_sig(39)], horizon=5)
    same = (0, 180 * DAY)
    text = R.render([_market("SOL-USDT-SWAP", "1D", early, window=same),
                     _market("ETH-USDT-SWAP", "1D", late, window=same)], 2.0, 30)
    assert "not directly comparable" not in text
    assert "| **all** | 1970-01-01 → 1970-06-30 |" in text


def test_candle_window_is_the_sampleable_range_not_the_raw_series():
    c = _candles([(110, 109, 110)] * 100)
    assert R.candle_window(c) == (0, 99 * DAY)                    # no padding asked for
    assert R.candle_window(c, warmup=60, horizon=30) == (60 * DAY, 69 * DAY)
    assert R.candle_window(_candles([])) is None
    # padding larger than the series must not invert the range
    short = _candles([(110, 109, 110)] * 3)
    lo, hi = R.candle_window(short, warmup=60, horizon=30)
    assert lo <= hi


def test_a_few_days_of_padding_difference_does_not_cry_wolf():
    """--days pads warm-up in BARS, so 1D and 4H data ranges differ by weeks even
    when the sampled period is the same 2 years. That must not read as 'mixed'."""
    two_years = 730 * DAY
    assert R.windows_comparable([(0, two_years), (25 * DAY, two_years)])
    # …but three years of 1D against six months of 4H is genuinely incomparable
    assert not R.windows_comparable([(0, 1095 * DAY), (912 * DAY, 1095 * DAY)])
    assert R.windows_comparable([])                               # nothing to compare
    assert R.windows_comparable([(0, two_years)])


def test_render_is_markdown_with_a_row_per_market_and_a_pooled_line():
    bars = [(110, 109, 110)] + [(131, 118, 130)]
    rows, _ = _eval(bars, [_sig(0)], horizon=5)
    text = R.render([_market("SOL-USDT-SWAP", "1D", rows),
                     _market("BTC-USDT-SWAP", "1D", [])], 2.0, 30)
    assert "| SOL-USDT-SWAP 1D |" in text and "| BTC-USDT-SWAP 1D |" in text
    assert "**all**" in text and "Pooled expectancy" in text
    assert "no look-ahead" in text
    assert "Not a strategy result" in text and "No costs" in text   # caveats travel with it


def test_every_row_carries_its_own_window():
    """A bar count spans a different period per timeframe, so each row says which."""
    bars = [(110, 109, 110)] + [(131, 118, 130)]
    rows, _ = _eval(bars, [_sig(0)], horizon=5)
    text = R.render([_market("SOL-USDT-SWAP", "1D", rows)], 2.0, 30)
    assert "| window |" in text
    assert "1970-01-01 → 1970-01-01" in text


def test_mixed_windows_are_flagged_and_not_pooled_silently():
    """The real case: --limit 1100 is ~3 years of 1D but ~6 months of 4H."""
    rows, _ = _eval([(110, 109, 110)] + [(131, 118, 130)], [_sig(0)], horizon=5)
    text = R.render([_market("SOL", "1D", rows, window=(0, 1095 * DAY)),
                     _market("SOL", "4H", rows, window=(912 * DAY, 1095 * DAY))], 2.0, 30)
    assert "not directly comparable" in text
    assert "| **all** | mixed |" in text
    assert "--days" in text                                  # points at the fix


def test_unresolved_signals_are_reported_not_hidden():
    bars = [(110, 109, 110), (112, 108, 111), (112, 108, 111)]
    rows, unresolved = _eval(bars, [_sig(0)], horizon=10)
    text = R.render([_market("SOL-USDT-SWAP", "1D", rows, unresolved)], 2.0, 10)
    assert unresolved == 1
    assert "fired too close to the end of the data" in text


def test_exit_time_is_when_the_trade_closed_not_when_it_opened():
    """analytics.closed() sequences the equity curve by closed_ts, so a signal that
    ran 30 bars must not be applied before one that stopped out the next day."""
    slow = {"time": "2026-01-01T00:00:00Z", "tf": "1D", "bars_held": 30}
    fast = {"time": "2026-01-05T00:00:00Z", "tf": "1D", "bars_held": 1}
    assert R._exit_ts(slow) > R._exit_ts(fast)             # fast closes first
    assert R._exit_ts(fast) - R._exit_ts({**fast, "bars_held": 0}) == 86_400_000
    assert R._exit_ts({**fast, "tf": "4H"}) - iso_to_ms(fast["time"]) == 4 * 3_600_000
    # an unknown bar string must not blow up
    assert R._exit_ts({**fast, "tf": "7Y"}) == iso_to_ms(fast["time"])


def test_synthetic_trades_close_when_they_actually_closed():
    bars = ([(110, 109, 110)] + [(112, 108, 111)] * 3 + [(131, 118, 130)]   # slow: 4 bars
            + [(110, 109, 110)] + [(113, 99, 101)])                        # fast: 1 bar
    rows, _ = _eval(bars, [_sig(0), _sig(5)], horizon=4)
    trades = analytics.closed(R.as_trades(rows))
    assert [r["bars_held"] for r in rows] == [4, 1]
    for t, r in zip(sorted(trades, key=lambda x: x.opened_ts), rows):
        assert t.closed_ts == t.opened_ts + r["bars_held"] * DAY
        assert t.closed_ts > t.opened_ts                  # never closes at its own entry


# ── engine params are disclosed ──────────────────────────────────────────────
def test_params_default_to_strict_and_the_report_says_so():
    params, label, exchange = R.load_params(None)
    assert params == Params() and label == "strict defaults" and exchange is None
    text = R.render([_market("SOL", "1D", [])], 2.0, 30, label)
    assert "strict defaults" in text


def test_params_can_come_from_a_config_and_are_named_in_the_report(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "exchange: binance\n"
        "watches:\n  - symbol: SOL-USDT-SWAP\n    timeframe: \"1D\"\n"
        "params:\n  atr_break: 0.75\n  min_touches: 4\n", encoding="utf-8")
    params, label, exchange = R.load_params(str(cfg))
    assert params.atr_break == 0.75 and params.min_touches == 4
    assert params != Params() and exchange == "binance"
    text = R.render([_market("SOL", "1D", [])], 2.0, 30, label)
    assert str(cfg) in text and "(= strict defaults)" not in text
    # a config that happens to match the defaults says so rather than implying tuning
    plain = tmp_path / "plain.yaml"
    plain.write_text("watches:\n  - symbol: SOL-USDT-SWAP\n    timeframe: \"1D\"\n", encoding="utf-8")
    _, plain_label, _ = R.load_params(str(plain))
    assert "(= strict defaults)" in plain_label


def test_bars_for_gives_each_timeframe_the_same_span():
    """The fix for comparing timeframes: 180 days is 180 bars of 1D but 1080 of 4H."""
    assert R.bars_for("1D", 180, warmup=0, horizon=0) == 180
    assert R.bars_for("4H", 180, warmup=0, horizon=0) == 180 * 6
    assert R.bars_for("1H", 10, warmup=0, horizon=0) == 240
    # warm-up and horizon are added on top, since neither yields judged signals
    assert R.bars_for("1D", 180, warmup=60, horizon=30) == 270
