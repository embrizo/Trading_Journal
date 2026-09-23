"""Weekly / monthly review: deterministic metrics block + optional LLM narrative.

``build_metrics`` is pure (numbers only, from ``analytics``); ``render`` turns
it into markdown; ``generate`` optionally adds a WEEKLY_V1 narrative via the
coach and stores everything in ``ai_analysis``. ``chart_png`` needs matplotlib
and degrades to ``None`` without it. ``next_run`` parses the cron-ish
``"MON 00:15"`` schedule for the in-process scheduler.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import analytics, memory, prompts
from .db import JournalDB, now_ms
from .models import Trade

if TYPE_CHECKING:
    from .coach import Coach

log = logging.getLogger(__name__)

_DAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
PERIOD_DAYS = {"weekly": 7, "monthly": 30}


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ms else None


def _f(x: Any, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, str):
        return x
    if x == float("inf"):
        return "inf"
    return f"{x:.{nd}f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


# ── metrics (pure) ──────────────────────────────────────────────────────────
def build_metrics(db: JournalDB, kind: str = "weekly", now_ms_: int | None = None) -> dict:
    """Everything the report shows, computed once, JSON-safe."""
    now = now_ms_ if now_ms_ is not None else now_ms()
    days = PERIOD_DAYS[kind]
    since = now - days * 86_400_000
    all_trades = db.list_trades()
    in_period = [t for t in all_trades if (t.closed_ts or t.opened_ts or 0) >= since]
    closed_period = analytics.closed(in_period)
    open_trades = [t for t in all_trades if t.status == "OPEN"]
    skipped_period = [t for t in in_period if t.status == "SKIPPED"]

    def brief(t: Trade) -> dict:
        return {"id": t.id, "symbol": t.symbol, "tf": t.tf, "direction": t.direction, "outcome": t.outcome,
                "r": t.r_multiple, "entry_tags": t.entry_tags, "exit_tags": t.exit_tags,
                "exit_reason": t.exit_reason, "closed": _iso(t.closed_ts)}

    with_r = [t for t in closed_period if t.r_multiple is not None]
    best = max(with_r, key=lambda t: t.r_multiple, default=None)
    worst = min(with_r, key=lambda t: t.r_multiple, default=None)
    violations = db.conn.execute(
        "SELECT r.name, COUNT(*) AS n, GROUP_CONCAT(v.trade_id) AS ids FROM rule_violations v "
        "JOIN rules r ON r.id = v.rule_id JOIN trades t ON t.id = v.trade_id "
        "WHERE COALESCE(t.closed_ts, t.opened_ts) >= ? GROUP BY r.id ORDER BY n DESC", (since,)).fetchall()
    signals_n = db.conn.execute("SELECT COUNT(*) FROM signals WHERE candle_ts >= ?", (since,)).fetchone()[0]
    return {
        "kind": kind, "period_days": days, "from": _iso(since), "to": _iso(now),
        "period": analytics.summarize(closed_period),
        "all_time": analytics.summarize(all_trades),
        "trades_closed": [brief(t) for t in closed_period],
        "best": brief(best) if best else None,
        "worst": brief(worst) if worst else None,
        "open_trades": [brief(t) for t in open_trades],
        "signals_in_period": int(signals_n),
        "skipped_in_period": len(skipped_period),
        "tag_stats_entry": analytics.tag_stats(closed_period, "ENTRY"),
        "tag_stats_exit": analytics.tag_stats(closed_period, "EXIT"),
        "by_tf": analytics.feature_stats(closed_period)["tf"],
        "by_direction": analytics.feature_stats(closed_period)["direction"],
        "rule_violations": [{"rule": r["name"], "n": r["n"],
                             "trade_ids": [int(x) for x in str(r["ids"]).split(",")]} for r in violations],
        "equity_curve_all_time": analytics.equity_curve(all_trades)[-30:],
        "memories": memory.list_memories(db),
    }


# ── markdown ────────────────────────────────────────────────────────────────
def _summary_lines(s: dict) -> list[str]:
    st = s["streaks"]
    return [
        f"- n={s['n']} · W/L/BE {s['wins']}/{s['losses']}/{s['be']} · win {_pct(s['win_rate'])} · "
        f"avg {_f(s['avg_r'])}R · total {_f(s['total_r'])}R · PF {_f(s['profit_factor'])} · "
        f"max DD {_f(s['max_drawdown_r'])}R",
        f"- streak: {st['current']} {st['current_kind'] or ''} (max win {st['max_win']}, max loss {st['max_loss']})",
    ]


def _bucket_table(groups: dict, title: str) -> list[str]:
    if not groups:
        return []
    lines = [f"**{title}**", "", "| bucket | n | W/L/BE | win | avg R |", "|---|---|---|---|---|"]
    for k, v in groups.items():
        lines.append(f"| {k} | {v['n']} | {v['wins']}/{v['losses']}/{v['be']} | {_pct(v['win_rate'])} | {_f(v['avg_r'])} |")
    return lines + [""]


def render(m: dict, narrative: str | None = None, unverified: list[str] | None = None) -> str:
    title = "Weekly review" if m["kind"] == "weekly" else "Monthly review"
    lines = [f"# {title} — {m['from']} → {m['to']}", ""]
    lines += ["## This period"] + _summary_lines(m["period"]) + [
        f"- alerts fired: {m['signals_in_period']} · deliberately skipped: {m['skipped_in_period']} · "
        f"open now: {len(m['open_trades'])}", ""]
    if m["best"]:
        b, w = m["best"], m["worst"]
        lines.append(f"- best: #{b['id']} {b['symbol']} {b['tf']} {b['direction']} {_f(b['r'])}R"
                     + (f" — {b['exit_reason']}" if b["exit_reason"] else ""))
        if w and w["id"] != b["id"]:
            lines.append(f"- worst: #{w['id']} {w['symbol']} {w['tf']} {w['direction']} {_f(w['r'])}R"
                         + (f" — {w['exit_reason']}" if w["exit_reason"] else ""))
        lines.append("")
    lines += _bucket_table(m["tag_stats_entry"], "By entry tag")
    lines += _bucket_table(m["by_tf"], "By timeframe")
    if m["rule_violations"]:
        lines += ["**Rule violations**", ""] + [
            f"- {v['rule']}: {v['n']}× ({', '.join(f'#{i}' for i in v['trade_ids'])})" for v in m["rule_violations"]
        ] + [""]
    lines += ["## All time"] + _summary_lines(m["all_time"]) + [""]
    if m["memories"]:
        lines += ["## Coach memory", ""] + [
            f"- {'✓ ' if x['confirmed'] else ''}{x['content']}" for x in m["memories"]] + [""]
    if narrative:
        lines += ["## Coach notes", "", narrative, ""]
        if unverified:
            lines.append(f"⚠ numbers not found in the metrics: {', '.join(unverified)}")
            lines.append("")
    lines.append(f"_metrics by analytics.py · {'narrative ' + prompts.WEEKLY_VERSION if narrative else 'no narrative'}_")
    return "\n".join(lines)


# ── chart (optional) ────────────────────────────────────────────────────────
def chart_png(m: dict) -> bytes | None:
    """Equity curve + entry-tag bar chart. ``None`` when matplotlib is missing
    or there is nothing to draw."""
    curve = m["equity_curve_all_time"]
    tags = m["tag_stats_entry"]
    if not curve and not tags:
        return None
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    fig, axes = plt.subplots(1, 2 if tags else 1, figsize=(10, 3.6))
    axes = list(axes) if tags else [axes]
    ax = axes[0]
    if curve:
        ax.plot(range(1, len(curve) + 1), [p["cum_r"] for p in curve], marker="o", ms=3)
        ax.axhline(0, color="grey", lw=0.8)
    ax.set_title("Equity (cumulative R, last 30 closed)")
    ax.set_xlabel("trade")
    if tags:
        ax2 = axes[1]
        names = list(tags)[:10]
        ax2.bar(names, [tags[n]["avg_r"] or 0 for n in names],
                color=["#2a9d8f" if (tags[n]["avg_r"] or 0) >= 0 else "#e76f51" for n in names])
        ax2.set_title("Avg R by entry tag (this period)")
        ax2.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return buf.getvalue()


# ── generate + store ────────────────────────────────────────────────────────
async def generate(db: JournalDB, kind: str = "weekly", coach: "Coach | None" = None,
                   store: bool = True, refresh_memory: bool = True, now_ms_: int | None = None) -> dict:
    """Metrics → (memories refreshed) → narrative (if a coach is given) → markdown.
    Stored in ``ai_analysis`` with kind weekly|monthly; ``input_metrics`` is the block."""
    if refresh_memory:
        memory.refresh(db, now_ms_=now_ms_)
    m = build_metrics(db, kind, now_ms_)
    narrative = None
    unverified: list[str] = []
    model = "none"
    if coach is not None:
        system = prompts.WEEKLY_V1
        payload = {k: v for k, v in m.items() if k not in ("equity_curve_all_time",)}
        narrative, unverified, model = await coach.narrative(system, payload)
    md = render(m, narrative, unverified)
    out = {"kind": kind, "metrics": m, "narrative": narrative, "unverified_numbers": unverified, "markdown": md}
    if store:
        cur = db.conn.execute(
            "INSERT INTO ai_analysis(trade_id, kind, model, prompt_version, input_metrics, output, created_ts) "
            "VALUES (NULL,?,?,?,?,?,?)",
            (kind, model, prompts.WEEKLY_VERSION if narrative else "metrics_only",
             json.dumps(m, ensure_ascii=False, default=str), md, now_ms()))
        db.conn.commit()
        out["analysis_id"] = int(cur.lastrowid)
    return out


# ── scheduling ──────────────────────────────────────────────────────────────
def parse_schedule(spec: str) -> tuple[int, int, int]:
    """``"MON 00:15"`` → (weekday 0-6, hour, minute), UTC."""
    try:
        day, hm = spec.strip().upper().split()
        h, mnt = (int(x) for x in hm.split(":"))
        wd = _DAYS[day[:3]]
    except (ValueError, KeyError) as e:
        raise ValueError(f"bad weekly_report_cron {spec!r} (expected e.g. 'MON 00:15')") from e
    if not (0 <= h < 24 and 0 <= mnt < 60):
        raise ValueError(f"bad time in {spec!r}")
    return wd, h, mnt


def next_run(spec: str, now: datetime | None = None) -> datetime:
    """Next UTC datetime matching the weekly spec, strictly after ``now``."""
    wd, h, mnt = parse_schedule(spec)
    now = now or datetime.now(timezone.utc)
    cand = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
    cand += timedelta(days=(wd - cand.weekday()) % 7)
    if cand <= now:
        cand += timedelta(days=7)
    return cand


def next_monthly(now: datetime | None = None, hour: int = 0, minute: int = 30) -> datetime:
    """First day of next month at hour:minute UTC."""
    now = now or datetime.now(timezone.utc)
    first = now.replace(day=1, hour=hour, minute=minute, second=0, microsecond=0)
    if first <= now:
        first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first


async def run_scheduler(cfg, db: JournalDB, notifiers: list, coach: "Coach | None") -> None:
    """Background task: weekly report at ``ai.weekly_report_cron``, monthly on the 1st."""
    if not cfg.ai.weekly_report:
        return
    while True:
        now = datetime.now(timezone.utc)
        weekly_at, monthly_at = next_run(cfg.ai.weekly_report_cron, now), next_monthly(now)
        kind, when = ("weekly", weekly_at) if weekly_at <= monthly_at else ("monthly", monthly_at)
        log.info("report scheduler: next %s report at %s UTC", kind, when.strftime("%Y-%m-%d %H:%M"))
        await asyncio.sleep(max(1.0, (when - datetime.now(timezone.utc)).total_seconds()))
        try:
            out = await generate(db, kind, coach)
            image = chart_png(out["metrics"])
            text = out["markdown"]
            for n in notifiers:
                try:
                    await n.send(text[:4000], image)
                except Exception:  # noqa: BLE001
                    log.exception("report push failed on %s", getattr(n, "name", n))
        except Exception:  # noqa: BLE001 — a failed report never kills the scheduler
            log.exception("%s report failed", kind)
