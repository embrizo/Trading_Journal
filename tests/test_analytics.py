"""Golden numbers on a fixture journal. Every expected value below was computed
by hand from the fixture — if this test fails, the *analytics* changed, not the
data."""
import pytest

from break_signal.journal import analytics as A
from break_signal.journal.db import JournalDB

# (direction, entry, sl, exit, entry_tags) → hand-computed R in the comment
FIXTURE = [
    ("LONG", 100, 90, 120, ["Breakout", "Retest"]),   # 1  +2.0  W
    ("LONG", 100, 90, 90, ["Breakout", "FOMO"]),      # 2  -1.0  L
    ("LONG", 100, 90, 115, ["Retest"]),               # 3  +1.5  W
    ("LONG", 100, 90, 85, ["FOMO"]),                  # 4  -1.5  L
    ("LONG", 100, 90, 100.5, ["Range"]),              # 5  +0.05 BE
    ("SHORT", 100, 110, 80, ["Breakout"]),            # 6  +2.0  W
    ("SHORT", 100, 110, 110, ["FOMO"]),               # 7  -1.0  L
    ("LONG", 100, 90, 130, ["Retest"]),               # 8  +3.0  W
    ("LONG", 100, 90, 95, ["Breakout"]),              # 9  -0.5  L
    ("LONG", 100, 90, 92, ["FOMO"]),                  # 10 -0.8  L
    ("LONG", 100, 90, 110, ["Retest", "Breakout"]),   # 11 +1.0  W
]
# Rs: 2, -1, 1.5, -1.5, .05, 2, -1, 3, -.5, -.8, 1
# wins 5 (sum 9.5) · losses 5 (sum -4.8) · be 1 · total 4.75
# cum: 2, 1, 2.5, 1.0, 1.05, 3.05, 2.05, 5.05, 4.55, 3.75, 4.75 → max DD 1.5 (2.5→1.0)
# sequence W L W L BE W L W L L W → max_win 1, max_loss 2, current 1 WIN


@pytest.fixture(scope="module")
def trades():
    db = JournalDB(":memory:")
    for i, (d, e, sl, x, tags) in enumerate(FIXTURE, start=1):
        t = db.add_trade("SOL-USDT-SWAP", d, tf="4H" if i % 2 else "1D", entry_price=e,
                         sl_price=sl, entry_tags=tags, opened_ts=i * 1_000, ctx_rsi=40 + i * 3)
        db.close_trade(t.id, x, closed_ts=i * 1_000 + 500,
                       exit_tags=["Hit TP"] if x > e and d == "LONG" else [])
    db.add_trade("SOL-USDT-SWAP", "LONG", entry_price=100, sl_price=90)  # 12: OPEN, ignored
    out = db.list_trades()
    db.close()
    return out


def test_r_multiple_arithmetic():
    assert A.r_multiple("LONG", 100, 90, 120) == pytest.approx(2.0)
    assert A.r_multiple("SHORT", 100, 110, 80) == pytest.approx(2.0)
    assert A.r_multiple("SHORT", 100, 110, 115) == pytest.approx(-1.5)
    assert A.r_multiple("LONG", 100, 100, 120) is None   # zero risk
    assert A.r_multiple("LONG", 100, None, 120) is None
    assert A.planned_rr("LONG", 100, 90, 125) == pytest.approx(2.5)


def test_no_r_when_the_stop_sits_beyond_entry():
    """A stop trailed past entry has negative risk; R would come out inverted —
    a winner as a big negative number, derived as a LOSS. R is measured against
    INITIAL risk, so such a stop yields no R at all."""
    # LONG with the stop above entry, SHORT with it below
    assert A.r_multiple("LONG", 1.2673, 1.27, 1.35) is None
    assert A.r_multiple("SHORT", 1.0276, 1.027, 0.95) is None
    # the same exits against the stops the trades were opened with
    assert A.r_multiple("LONG", 1.2673, 1.133, 1.35) == pytest.approx(0.6158, abs=1e-4)
    assert A.r_multiple("SHORT", 1.0276, 1.082, 0.95) == pytest.approx(1.4265, abs=1e-4)
    # planned_rr shares the guard
    assert A.planned_rr("LONG", 100, 105, 130) is None
    assert A.pnl_amount("LONG", 1.2673, 1.35, None, 17.3, None, None) is None   # no R, no fallback


def test_pnl_prefers_size_then_risk_amount():
    assert A.pnl_amount("LONG", 100, 110, 2, None, None, 1) == pytest.approx(19)
    assert A.pnl_amount("SHORT", 100, 110, 2, None, None, 0) == pytest.approx(-20)
    assert A.pnl_amount("LONG", 100, 110, None, 50, 1.0, 0) == pytest.approx(50)
    assert A.pnl_amount("LONG", 100, 110, None, None, 1.0, 0) is None


def test_derive_outcome_threshold():
    assert A.derive_outcome(0.5) == "WIN"
    assert A.derive_outcome(-0.5) == "LOSS"
    assert A.derive_outcome(0.09) == "BE"
    assert A.derive_outcome(-0.1) == "BE"
    assert A.derive_outcome(None) is None


def test_bands_and_session():
    assert [A.rsi_band(x) for x in (10, 30, 50, 70, None)] == ["<30", "30-50", "50-70", ">70", None]
    assert [A.atr_dist_band(x) for x in (0.2, 0.7, 1.5)] == ["<0.5", "0.5-1", ">1"]
    hour = 3_600_000
    assert A.session_of(0) == "ASIA"
    assert A.session_of(9 * hour) == "LONDON"
    assert A.session_of(15 * hour) == "NY"
    assert A.session_of(23 * hour) == "ASIA"


def test_period_to_since():
    now = 100 * 86_400_000
    assert A.period_to_since("30d", now) == 70 * 86_400_000
    assert A.period_to_since("2w", now) == 86 * 86_400_000
    assert A.period_to_since("all", now) is None
    assert A.period_to_since(None, now) is None
    with pytest.raises(ValueError):
        A.period_to_since("soon", now)


def test_summary_golden(trades):
    s = A.summarize(trades)
    assert s["n"] == 11
    assert (s["wins"], s["losses"], s["be"]) == (5, 5, 1)
    assert s["win_rate"] == pytest.approx(5 / 11, abs=1e-3)
    assert s["total_r"] == pytest.approx(4.75)
    assert s["avg_r"] == pytest.approx(4.75 / 11, abs=1e-3)
    assert s["profit_factor"] == pytest.approx(9.5 / 4.8, abs=1e-3)
    assert s["avg_win_r"] == pytest.approx(1.9)
    assert s["avg_loss_r"] == pytest.approx(-0.96)
    assert s["max_drawdown_r"] == pytest.approx(1.5)
    assert s["streaks"] == {"max_win": 1, "max_loss": 2, "current": 1, "current_kind": "WIN"}
    assert s["total_pnl"] is None and s["pnl_n"] == 0


def test_equity_curve_order_and_values(trades):
    curve = A.equity_curve(trades)
    assert [p["cum_r"] for p in curve] == pytest.approx(
        [2, 1, 2.5, 1.0, 1.05, 3.05, 2.05, 5.05, 4.55, 3.75, 4.75])
    assert [p["trade_id"] for p in curve] == list(range(1, 12))


def test_empty_summary():
    s = A.summarize([])
    assert s["n"] == 0 and s["win_rate"] is None and s["profit_factor"] is None
    assert s["streaks"]["current"] == 0


def test_counts_follow_stored_outcome_not_r():
    """A user-overridden outcome wins over the sign of R; R-based figures still use R."""
    db = JournalDB(":memory:")
    a = db.add_trade("SOL-USDT-SWAP", "LONG", entry_price=100, sl_price=90, opened_ts=1)
    db.close_trade(a.id, 105, outcome="BE", closed_ts=2)          # R=+0.5 but declared BE
    b = db.add_trade("SOL-USDT-SWAP", "LONG", entry_price=100, sl_price=90, opened_ts=3)
    db.close_trade(b.id, 120, closed_ts=4)                        # derived WIN, R=+2
    c = db.add_trade("SOL-USDT-SWAP", "LONG", entry_price=100, opened_ts=5)   # no stop → no R
    db.close_trade(c.id, 130, outcome="win", closed_ts=6)
    s = A.summarize(db.list_trades())
    assert (s["n"], s["r_n"]) == (3, 2)
    assert (s["wins"], s["losses"], s["be"]) == (2, 0, 1)
    assert s["win_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert s["avg_r"] == pytest.approx(1.25)                      # (0.5 + 2) / 2
    assert s["avg_win_r"] == pytest.approx(2.0)                   # only the WIN with an R
    assert s["streaks"] == {"max_win": 2, "max_loss": 0, "current": 2, "current_kind": "WIN"}
    assert [p["trade_id"] for p in A.equity_curve(db.list_trades())] == [a.id, b.id]
    assert len(db.list_trades(outcome="WIN")) == s["wins"]        # search and stats agree
    db.close()


def test_pf_ignores_sign_contradicting_overrides():
    """A LOSS declared on a +R exit must not shrink gross loss (which made PF 'inf')."""
    db = JournalDB(":memory:")
    a = db.add_trade("SOL-USDT-SWAP", "LONG", entry_price=100, sl_price=90, opened_ts=1)
    db.close_trade(a.id, 120, closed_ts=2)                       # WIN +2
    b = db.add_trade("SOL-USDT-SWAP", "LONG", entry_price=100, sl_price=90, opened_ts=3)
    db.close_trade(b.id, 90, closed_ts=4)                        # LOSS -1
    c = db.add_trade("SOL-USDT-SWAP", "LONG", entry_price=100, sl_price=90, opened_ts=5)
    db.close_trade(c.id, 115, outcome="LOSS", closed_ts=6)       # declared LOSS at +1.5
    s = A.summarize(db.list_trades())
    assert (s["wins"], s["losses"]) == (1, 2)
    assert s["profit_factor"] == pytest.approx(2.0)              # 2 / |-1|; the +1.5 "loss" is ignored
    assert s["total_r"] == pytest.approx(2.5)                    # but it still counts in total R
    db.close()


def test_tag_in_both_phases_counted_once():
    db = JournalDB(":memory:")
    t = db.add_trade("SOL-USDT-SWAP", "LONG", entry_price=100, sl_price=90, entry_tags=["Breakout"])
    db.close_trade(t.id, 110, exit_tags=["Breakout"])
    assert A.tag_stats(db.list_trades())["Breakout"]["n"] == 1
    assert A.tag_stats(db.list_trades(), "ENTRY")["Breakout"]["n"] == 1
    assert A.tag_stats(db.list_trades(), "EXIT")["Breakout"]["n"] == 1
    db.close()


def test_tag_stats_golden(trades):
    ts = A.tag_stats(trades, "ENTRY")
    assert list(ts)[:1] == ["Breakout"]  # largest n first
    bo = ts["Breakout"]
    assert bo["n"] == 5 and bo["wins"] == 3 and bo["losses"] == 2
    assert bo["avg_r"] == pytest.approx(0.7)
    assert bo["profit_factor"] == pytest.approx(5 / 1.5, abs=1e-3)
    rt = ts["Retest"]
    assert rt["n"] == 4 and rt["wins"] == 4 and rt["profit_factor"] == float("inf")
    assert rt["avg_r"] == pytest.approx(1.875)
    fo = ts["FOMO"]
    assert fo["n"] == 4 and fo["losses"] == 4 and fo["profit_factor"] == 0.0
    assert fo["avg_r"] == pytest.approx(-1.075)
    rg = ts["Range"]
    assert rg["n"] == 1 and rg["be"] == 1 and rg["profit_factor"] is None
    # exit-phase tags are separate
    assert "Hit TP" in A.tag_stats(trades, "EXIT")
    assert "Hit TP" not in ts
    assert A.tag_stats(trades)["Hit TP"]["n"] == 5


def test_feature_stats_golden(trades):
    f = A.feature_stats(trades)
    assert f["direction"]["LONG"]["n"] == 9
    sh = f["direction"]["SHORT"]
    assert sh["n"] == 2 and sh["wins"] == 1 and sh["losses"] == 1
    assert sh["avg_r"] == pytest.approx(0.5) and sh["profit_factor"] == pytest.approx(2.0)
    assert f["tf"]["4H"]["n"] == 6 and f["tf"]["1D"]["n"] == 5
    assert set(f["rsi_band"]) == {"30-50", "50-70", ">70"}
    assert f["signal_linked"] == {"discretionary": A.summarize(trades)}
    assert f["signal_side"] == {}
