"""Phase J4: coach memory, weekly report, schedule parsing, schema migration v2."""
import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from break_signal.journal import memory, report
from break_signal.journal.db import SCHEMA_VERSION, JournalDB
from break_signal.journal.footer import alert_footer
from break_signal.journal.tools import Tools

DAY = 86_400_000
NOW = 1_800_000_000_000  # fixed "now" (2027-01-15) so period windows are deterministic


def _seed(tools: Tools, now=NOW):
    """6 FOMO losses, 6 Retest wins (all within 30 d), 2 old trades, 4 risk-rule breaks."""
    t = tools
    i = 0
    for tags, exit_, risk in [(["FOMO"], 90, 3.0)] * 6 + [(["Retest"], 120, 0.5)] * 6:
        i += 1
        opened = now - i * DAY
        tid = t.db.add_trade("SOL-USDT-SWAP", "LONG", tf="4H", entry_price=100, sl_price=90, tp_price=120,
                             entry_tags=tags, risk_pct=risk, opened_ts=opened, ctx_rsi=60).id
        t.db.close_trade(tid, exit_, closed_ts=opened + DAY // 2)
        from break_signal.journal import rules
        rules.record_violations(t.db, tid, rules.check(t.db, t.db.get_trade(tid)))
    for k in range(2):  # outside 90 d
        opened = now - (100 + k) * DAY
        tid = t.db.add_trade("SOL-USDT-SWAP", "LONG", tf="1D", entry_price=100, sl_price=90,
                             entry_tags=["FOMO"], opened_ts=opened).id
        t.db.close_trade(tid, 130, closed_ts=opened + DAY)


@pytest.fixture
def tools():
    t = Tools(JournalDB(":memory:"))
    _seed(t)
    yield t
    t.db.close()


# ── migration ────────────────────────────────────────────────────────────────
def test_v1_database_migrates_to_current(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL, applied_ts INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1, 0);
        CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, content TEXT NOT NULL,
            evidence TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
            first_seen_ts INTEGER NOT NULL, last_seen_ts INTEGER NOT NULL);
        INSERT INTO memories(type, content, evidence, confirmed, first_seen_ts, last_seen_ts)
            VALUES ('preference', 'likes 4H', '{}', 1, 0, 0);
    """)
    conn.commit(); conn.close()
    db = JournalDB(path)
    assert db.schema_version == SCHEMA_VERSION
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(memories)")}
    assert "key" in cols                                             # v2 step ran
    assert memory.list_memories(db)[0]["content"] == "likes 4H"      # data preserved
    versions = [r[0] for r in db.conn.execute("SELECT version FROM schema_version ORDER BY version")]
    assert versions == list(range(1, SCHEMA_VERSION + 1))            # every step recorded, in order
    db.close()
    db2 = JournalDB(path)                                            # idempotent re-open
    assert db2.schema_version == SCHEMA_VERSION
    db2.close()


def test_fresh_db_seeds_rules_once(tmp_path):
    from break_signal.journal import rules
    path = tmp_path / "j.db"
    db = JournalDB(path)
    assert len(rules.list_rules(db)) == len(rules.SEED_RULES)
    rules.delete_rule(db, rules.list_rules(db)[0]["id"])
    db.close()
    db = JournalDB(path)
    assert len(rules.list_rules(db)) == len(rules.SEED_RULES) - 1   # deletion sticks
    db.close()


# ── memory ───────────────────────────────────────────────────────────────────
def test_derive_patterns_and_rules(tools):
    ms = {m["key"]: m for m in memory.derive(tools.db, now_ms_=NOW)}
    assert "tag:fomo" in ms and "has not worked" in ms["tag:fomo"]["content"]
    assert ms["tag:fomo"]["evidence"]["n"] == 6 and "6 W" not in ms["tag:fomo"]["content"]
    assert "6 trades in the last 90d, 0 W / 6 L" in ms["tag:fomo"]["content"]
    assert "tag:retest" in ms and "has worked" in ms["tag:retest"]["content"]
    assert "tf:4H" not in ms                                # 12 trades at 50% → not lopsided
    assert "rule:1" in ms and "broken 6 times" in ms["rule:1"]["content"]   # Max risk 1%
    assert "direction:LONG" not in ms
    # the two old FOMO wins are outside the 90 d window
    assert set(ms["tag:fomo"]["evidence"]["trade_ids"]) == {1, 2, 3, 4, 5, 6}


def test_refresh_upserts_and_prunes(tools):
    r = memory.refresh(tools.db, now_ms_=NOW)
    assert r["added"] >= 3 and r["updated"] == 0 and r["removed"] == 0
    first = {m["key"]: m for m in memory.list_memories(tools.db)}
    fomo = first["tag:fomo"]
    memory.confirm(tools.db, fomo["id"])
    r2 = memory.refresh(tools.db, now_ms_=NOW)
    assert r2["added"] == 0 and r2["updated"] == r["added"]
    second = {m["key"]: m for m in memory.list_memories(tools.db)}
    assert second["tag:fomo"]["id"] == fomo["id"] and second["tag:fomo"]["confirmed"]
    # 200 days later nothing is in the window: unconfirmed rows go, the confirmed one stays
    r3 = memory.refresh(tools.db, now_ms_=NOW + 200 * DAY)
    assert r3["removed"] >= 2
    left = memory.list_memories(tools.db)
    assert [m["key"] for m in left] == ["tag:fomo"] and left[0]["confirmed"]


def test_confirm_forget_note(tools):
    with pytest.raises(KeyError):
        memory.confirm(tools.db, 999)
    n = memory.add_note(tools.db, "I call a failed breakout a 'fakeout'", "terminology")
    assert n["confirmed"] and n["key"] is None
    memory.refresh(tools.db, now_ms_=NOW + 400 * DAY)        # notes are never pruned
    assert any(m["id"] == n["id"] for m in memory.list_memories(tools.db))
    memory.forget(tools.db, n["id"])
    with pytest.raises(KeyError):
        memory.forget(tools.db, n["id"])
    assert "no memories yet" in memory.format_memories([])


# ── report ───────────────────────────────────────────────────────────────────
def test_build_metrics_windows(tools):
    m = report.build_metrics(tools.db, "weekly", NOW)
    assert m["period"]["n"] == 7 and m["all_time"]["n"] == 14      # closes at opened+12h: i=1..7
    assert m["best"]["r"] == 2.0 and m["worst"]["r"] == -1.0
    assert {v["rule"] for v in m["rule_violations"]} == {"Max risk 1%", "No FOMO entries"}
    assert all(v["n"] == 6 for v in m["rule_violations"])
    assert m["from"] == "2027-01-08" and m["to"] == "2027-01-15"
    mm = report.build_metrics(tools.db, "monthly", NOW)
    assert mm["period"]["n"] == 12
    assert "tag:fomo" not in {x["key"] for x in m["memories"]}   # not refreshed by build_metrics


def test_generate_markdown_and_store(tools):
    out = asyncio.run(report.generate(tools.db, "weekly", coach=None, now_ms_=NOW))
    md = out["markdown"]
    assert md.startswith("# Weekly review — 2027-01-08 → 2027-01-15")
    assert "n=7" in md and "| FOMO |" in md and "Max risk 1%" in md
    assert "## Coach memory" in md and "has not worked" in md    # generate() refreshed memories
    assert "no narrative" in md and "Coach notes" not in md
    row = tools.db.conn.execute("SELECT id, kind, model, prompt_version FROM ai_analysis").fetchone()
    assert tuple(row) == (out["analysis_id"], "weekly", "none", "metrics_only")


def test_generate_with_fake_coach(tools):
    class FakeCoach:
        cfg = type("C", (), {"model": "fake-model"})()

        async def narrative(self, system, payload):
            assert "weekly review" in system.lower()
            assert payload["period"]["n"] == 7
            return "THIS WEEK: 7 trades, 1 W. ONE THING TO WATCH: FOMO 6 L.", ["99"], "fake-model"
    out = asyncio.run(report.generate(tools.db, "weekly", FakeCoach(), now_ms_=NOW))
    assert "## Coach notes" in out["markdown"] and "⚠ numbers not found in the metrics: 99" in out["markdown"]
    row = tools.db.conn.execute("SELECT model, prompt_version FROM ai_analysis").fetchone()
    assert tuple(row) == ("fake-model", "weekly_v1")


def test_chart_png_optional(tools):
    m = report.build_metrics(tools.db, "weekly", NOW)
    png = report.chart_png(m)
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        assert png is None
    else:
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert report.chart_png({"equity_curve_all_time": [], "tag_stats_entry": {}}) is None


def test_schedule_parsing():
    assert report.parse_schedule("MON 00:15") == (0, 0, 15)
    assert report.parse_schedule("sunday 23:59") == (6, 23, 59)
    for bad in ("MON", "FUNDAY 00:00", "MON 25:00", "MON 00:60"):
        with pytest.raises(ValueError):
            report.parse_schedule(bad)
    wed = datetime(2027, 1, 13, 12, 0, tzinfo=timezone.utc)             # a Wednesday
    assert report.next_run("MON 00:15", wed) == datetime(2027, 1, 18, 0, 15, tzinfo=timezone.utc)
    assert report.next_run("WED 12:00", wed) == datetime(2027, 1, 20, 12, 0, tzinfo=timezone.utc)  # strictly after
    assert report.next_run("WED 12:01", wed) == datetime(2027, 1, 13, 12, 1, tzinfo=timezone.utc)
    assert report.next_monthly(wed) == datetime(2027, 2, 1, 0, 30, tzinfo=timezone.utc)
    assert report.next_monthly(datetime(2027, 12, 31, 23, 0, tzinfo=timezone.utc)) == \
        datetime(2028, 1, 1, 0, 30, tzinfo=timezone.utc)


# ── footer rule hint ─────────────────────────────────────────────────────────
def test_footer_shows_context_rule_breaks(tools):
    sig = dict(symbol="SOL-USDT-SWAP", exchange="OKX", tf="1D", event="break_up", side="resistance",
               price=1, line=1, atr_dist=0.4, touches=3, age_bars=1, vol_ratio=1.5, rsi=80.0,
               time="2027-01-15T00:00:00Z", line_id="x")
    f = alert_footer(tools.db, sig, 1)
    assert "⚠ Rules: No 1D entry when RSI > 75" in f
    sig["rsi"] = 60.0
    assert "⚠ Rules" not in alert_footer(tools.db, sig, 1)


# ── tool surface ─────────────────────────────────────────────────────────────
def test_tools_memory_and_report(tools):
    ms = tools.memories(refresh=True)
    assert any(m["key"] == "tag:fomo" for m in ms)
    mid = ms[0]["id"]
    assert tools.confirm_memory(mid)["confirmed"] is True
    assert tools.forget_memory(mid) == {"deleted": mid}
    note = tools.add_memory_note("prefers 4H over 1D")
    assert note["type"] == "preference" and note["confirmed"]
    r = tools.report("weekly")
    assert r["metrics"]["period"]["n"] >= 0 and r["markdown"].startswith("# Weekly review")
    import json
    json.dumps(r)
