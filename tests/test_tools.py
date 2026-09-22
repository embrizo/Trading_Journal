import json

import pytest

from break_signal.core.params import Params
from break_signal.journal.db import JournalDB
from break_signal.journal.tools import Tools, snapshot_from_candles

from .helpers import append_bar, descending_resistance

SIG = dict(symbol="SOL-USDT-SWAP", exchange="OKX", tf="4H", event="break_up", side="resistance",
           price=231.5, line=229.8, atr_dist=0.35, touches=4, age_bars=51, vol_ratio=1.9, rsi=61.3,
           time="2026-09-20T04:00:00Z", line_id="R:1:2")


@pytest.fixture
def tools():
    t = Tools(JournalDB(":memory:"))
    yield t
    t.db.close()


def _roundtrip(x):
    return json.loads(json.dumps(x, ensure_ascii=False))


# ── write path ───────────────────────────────────────────────────────────────
def test_add_trade_only_stated_fields_and_json(tools):
    out = tools.add_trade("sol", "long", entry_price=231.5, sl_price=225, tf="4H",
                          tags=["Breakout", "ตามวินัย"], entry_reason="clean retest")
    d = _roundtrip(out)
    t = d["trade"]
    assert t["symbol"] == "SOL-USDT-SWAP" and t["direction"] == "LONG"
    assert t["tp_price"] is None and t["planned_rr"] is None
    assert t["entry_tags"] == ["Breakout", "ตามวินัย"]
    assert d["auto_linked_signal"] is None
    assert [v["name"] for v in d["rule_violations"]] == []   # nothing applicable yet


def test_add_trade_auto_links_recent_signal_and_copies_ctx(tools):
    import time
    fresh = dict(SIG)
    fresh["time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    sid = tools.db.insert_signal(fresh, "live")
    out = tools.add_trade("SOL", "LONG", entry_price=231.5, sl_price=225, tf="4H")
    assert out["auto_linked_signal"] == sid
    assert out["trade"]["signal_id"] == sid
    assert out["trade"]["ctx_rsi"] == 61.3 and out["trade"]["ctx_atr_dist"] == 0.35
    # a SHORT does not link to a break_up
    out2 = tools.add_trade("SOL", "SHORT", entry_price=231.5, sl_price=240, tf="4H")
    assert out2["auto_linked_signal"] is None
    # explicit off
    out3 = tools.add_trade("SOL", "LONG", tf="4H", auto_link=False)
    assert out3["auto_linked_signal"] is None


def test_add_trade_unknown_tf_skips_autolink_but_logs(tools):
    out = tools.add_trade("SOL", "LONG", tf="8H", entry_price=100, sl_price=95)
    assert out["trade"]["tf"] == "8H" and out["auto_linked_signal"] is None


def test_add_trade_explicit_signal_copies_ctx_and_validates(tools):
    sid = tools.db.insert_signal({**SIG, "time": "2026-01-01T00:00:00Z"}, "backtest")
    out = tools.add_trade("SOL", "LONG", tf="4H", entry_price=100, sl_price=95, signal_id=sid)
    assert out["trade"]["signal_id"] == sid and out["auto_linked_signal"] is None
    assert out["trade"]["ctx_rsi"] == 61.3 and out["trade"]["ctx_atr_dist"] == 0.35
    with pytest.raises(KeyError):
        tools.add_trade("SOL", "LONG", signal_id=999)


def test_json_keeps_inf_profit_factor():
    from break_signal.journal.tools import _json
    assert _json({"pf": float("inf"), "x": float("nan"), "y": 1.5}) == {"pf": "inf", "x": None, "y": 1.5}


def test_add_trade_old_signal_not_linked(tools):
    tools.db.insert_signal({**SIG, "time": "2026-01-01T00:00:00Z"}, "backtest")  # far outside 3 bars
    out = tools.add_trade("SOL", "LONG", tf="4H")
    assert out["auto_linked_signal"] is None


def test_line_variants_and_close(tools):
    a = tools.add_trade_line("SOL 4H long 231.5 sl 225 tp 245 #breakout -- felt calm")
    tid = a["trade"]["id"]
    assert a["trade"]["planned_rr"] == pytest.approx((245 - 231.5) / 6.5)
    c = tools.close_trade_line(f"{tid} 244 hit TP #hit_tp")
    assert c["trade"]["outcome"] == "WIN"
    assert c["trade"]["r_multiple"] == pytest.approx((244 - 231.5) / 6.5)
    assert c["trade"]["exit_tags"] == ["Hit TP"]
    assert c["trade"]["exit_reason"] == "hit TP"
    full = tools.get_trade(tid)
    assert full["rule_violations"] == []
    assert _roundtrip(full)["closed"] is not None


def test_close_outcome_conflict_is_flagged(tools):
    tid = tools.add_trade("SOL", "SHORT", entry_price=63.6, sl_price=67.5, auto_link=False)["trade"]["id"]
    out = tools.close_trade(tid, 60, outcome="loss")          # +0.92R declared a loss
    assert out["trade"]["outcome"] == "LOSS"
    assert "declared LOSS but R is +0.92" in out["outcome_note"]
    tid2 = tools.add_trade("SOL", "LONG", entry_price=100, sl_price=90, auto_link=False)["trade"]["id"]
    assert "outcome_note" not in tools.close_trade(tid2, 120, outcome="win")   # agrees → no note
    tid3 = tools.add_trade("SOL", "LONG", entry_price=100, sl_price=90, auto_link=False)["trade"]["id"]
    assert "outcome_note" not in tools.close_trade(tid3, 120)                  # derived → no note


def test_rule_violations_recorded_on_write(tools):
    out = tools.add_trade("SOL", "LONG", entry_price=100, sl_price=90, tp_price=101, risk_pct=3,
                          tags=["FOMO"])
    names = {v["name"] for v in out["rule_violations"]}
    assert names == {"Max risk 1%", "Planned R:R >= 1.5", "No FOMO entries"}
    tid = out["trade"]["id"]
    assert {v["name"] for v in tools.get_trade(tid)["rule_violations"]} == names
    ev = tools.add_event(tid, "sl_moved", {"from": 90, "to": 85})
    assert "Never widen the stop" in {v["name"] for v in ev["rule_violations"]}


def test_skip_signal_and_recent_signals(tools):
    sid = tools.db.insert_signal(SIG, "live")
    out = tools.skip_signal(sid, reason="RSI too high", tags=["Hesitation"])
    assert out["trade"]["status"] == "SKIPPED" and out["trade"]["signal_id"] == sid
    sigs = tools.recent_signals("sol")
    assert len(sigs) == 1 and sigs[0]["candle"] == "2026-09-20T04:00:00Z"
    assert tools.search_trades(status="SKIPPED")["n"] == 1


def test_update_trade_rechecks_the_rules():
    """A risk stated after the trade was logged must still break the max-risk rule —
    update_trade used to be the one write path that skipped rules.check()."""
    db = JournalDB(":memory:", account_size=331.31)
    t = Tools(db)
    tid = t.add_trade("SOL", "LONG", entry_price=100, sl_price=95)["trade"]["id"]
    assert t.get_trade(tid)["rule_violations"] == []

    out = t.update_trade(tid, risk_amount=34.6)          # 10.4% of the account
    assert out["trade"]["risk_pct"] == pytest.approx(10.44, abs=0.01)
    assert "Max risk 1%" in {v["name"] for v in out["rule_violations"]}
    # and it is persisted, not just returned
    assert "Max risk 1%" in {v["name"] for v in t.get_trade(tid)["rule_violations"]}

    out = t.update_trade(tid, risk_amount=2.0)           # back under 1%
    assert "Max risk 1%" not in {v["name"] for v in out["rule_violations"]}
    assert t.get_trade(tid)["rule_violations"] == []     # stale violation cleared
    db.close()


def test_tags_rules_update(tools):
    assert tools.add_tag("Sweep Reversal", "SETUP")["category"] == "SETUP"
    assert any(t["name"] == "Sweep Reversal" for t in tools.list_tags())
    tid = tools.add_trade("SOL", "LONG")["trade"]["id"]
    assert tools.tag_trade(tid, ["Sweep Reversal"], "ENTRY")["trade"]["entry_tags"] == ["Sweep Reversal"]
    assert tools.update_trade(tid, notes="n", confidence=4)["trade"]["confidence"] == 4
    assert "rule_violations" in tools.update_trade(tid, notes="n2")
    r = tools.add_rule("4H only", {"field": "tf", "op": "==", "value": "4H"}, "low")
    assert any(x["name"] == "4H only" for x in tools.list_rules())
    tools.set_rule_enabled(r["id"], False)
    assert next(x for x in tools.list_rules() if x["id"] == r["id"])["enabled"] is False


# ── read / calc path ─────────────────────────────────────────────────────────
@pytest.fixture
def seeded(tools):
    for i, (d, x, tags) in enumerate([("LONG", 120, ["Breakout"]), ("LONG", 90, ["FOMO"]),
                                      ("SHORT", 80, ["Breakout"]), ("LONG", 115, ["Retest"])], 1):
        sl = 90 if d == "LONG" else 110
        t = tools.add_trade("SOL", d, tf="4H", entry_price=100, sl_price=sl, tags=tags,
                            auto_link=False)["trade"]
        tools.close_trade(t["id"], x)
    return tools


def test_stats_and_groupings(seeded):
    s = seeded.stats()
    assert s["n"] == 4 and s["wins"] == 3 and s["losses"] == 1
    assert seeded.stats(direction="short")["n"] == 1
    assert seeded.stats(tags=["Breakout"])["n"] == 2
    ts = seeded.tag_stats(phase="ENTRY")["tags"]
    assert ts["Breakout"]["n"] == 2 and ts["FOMO"]["losses"] == 1
    f = seeded.feature_stats()
    assert f["direction"]["LONG"]["n"] == 3
    assert [p["trade_id"] for p in seeded.equity_curve()] == [1, 2, 3, 4]
    _roundtrip(s); _roundtrip(ts); _roundtrip(f)


def test_similar_and_review_context(seeded):
    res = seeded.similar_trades("sol", "long", tf="4H", tags=["Breakout"], k=2)
    assert [m["id"] for m in res["matches"]] == [1, 4] or [m["id"] for m in res["matches"]] == [1, 2]
    assert res["matches"][0]["id"] == 1 and res["aggregate"]["n"] == 2
    rc = seeded.review_context(1)
    assert rc["trade"]["id"] == 1
    assert rc["same_setup_stats"]["n"] == 2          # the other two LONG 4H SOL trades
    assert 1 not in [m["id"] for m in rc["similar"]["matches"]]
    assert "Breakout" in rc["tag_stats_entry"]
    assert "rules" in rc
    _roundtrip(rc)
    assert "error" in seeded.review_context(999)


def test_rule_check_proposed(tools):
    out = tools.rule_check({"symbol": "SOL-USDT-SWAP", "direction": "LONG", "tf": "1D",
                            "entry_price": 100, "sl_price": 95, "tp_price": 101, "ctx_rsi": 80})
    assert {v["name"] for v in out["violations"]} == {"Planned R:R >= 1.5", "No 1D entry when RSI > 75"}


# ── market snapshot (engine on synthetic candles, no network) ────────────────
def test_snapshot_from_candles_lines_and_signal():
    c = descending_resistance()
    last = len(c)
    line_val = 100.0 + (-0.2) * last
    c = append_bar(c, open_=line_val + 0.2, high=line_val + 5.5, low=line_val - 0.5,
                   close=line_val + 5.0, volume=300.0)
    snap = snapshot_from_candles("SOL-USDT-SWAP", "1D", c, Params())
    d = _roundtrip(snap)
    assert d["symbol"] == "SOL-USDT-SWAP" and d["tf"] == "1D" and d["candles"] == len(c)
    assert d["price"] == pytest.approx(line_val + 5.0)
    assert d["atr"] > 0 and d["rsi_band"] in ("<30", "30-50", "50-70", ">70")
    assert d["vol_ratio"] == pytest.approx(300 / ((19 * 100 + 300) / 20), rel=1e-3)
    assert d["last_bar_signal"] is not None and d["last_bar_signal"]["event"] == "break_up"
    assert any(l["side"] == "resistance" for l in d["lines"])
    r = next(l for l in d["lines"] if l["side"] == "resistance")
    assert r["touches"] >= 3 and r["dist_atr"] < 0   # line is now below price
    assert d["nearest_resistance_above"] is None


def test_snapshot_too_few_candles():
    c = descending_resistance(n=10, pivots=(5,))
    assert "error" in snapshot_from_candles("SOL-USDT-SWAP", "1D", c, Params())
