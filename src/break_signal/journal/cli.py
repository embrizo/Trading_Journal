"""``python -m break_signal.journal <command>`` — shell front-end to the journal.

Commands: add, close, skip, event, tag, list, show, signals, stats, export.
Resolves the DB path and symbol aliases from ``config.yaml`` when present
(``--config``), else falls back to ``data/journal.db``.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import analytics
from .db import JournalDB
from .export import summary_block
from .models import TAG_CATEGORIES, Trade
from .parser import ParseError
from .tools import Tools

DEFAULT_DB = "data/journal.db"


# ── formatting helpers ───────────────────────────────────────────────────────
def _ts(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _f(x: float | None, nd: int = 2) -> str:
    if x is None:
        return "-"
    if x == float("inf"):
        return "inf"
    return f"{x:.{nd}f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [max(len(str(c)) for c in col) for col in zip(header, *rows)] if rows else [len(h) for h in header]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*header), fmt.format(*["-" * w for w in widths])]
    lines += [fmt.format(*[str(c) for c in r]) for r in rows]
    return "\n".join(lines)


def _trade_line(t: Trade) -> list[str]:
    return [
        f"#{t.id}", t.status, t.symbol, t.tf or "-", t.direction,
        _f(t.entry_price, 4), _f(t.sl_price, 4), _f(t.tp_price, 4), _f(t.exit_price, 4),
        t.outcome or "-", _f(t.r_multiple), _ts(t.opened_ts),
        " ".join(f"#{x}" for x in t.tags),
    ]


_TRADE_HEADER = ["id", "status", "symbol", "tf", "dir", "entry", "sl", "tp", "exit",
                 "outcome", "R", "opened (UTC)", "tags"]


_summary_block = summary_block   # shared with export.py


def _group_table(groups: dict[str, dict]) -> str:
    rows = [[k, v["n"], f"{v['wins']}/{v['losses']}/{v['be']}", _pct(v["win_rate"]),
             _f(v["avg_r"]), _f(v["profit_factor"])]
            for k, v in groups.items()]
    return _table(rows, ["bucket", "n", "W/L/BE", "win%", "avg_R", "PF"])


# ── command handlers ─────────────────────────────────────────────────────────
def _print_violations(vs: list[dict]) -> None:
    for v in vs:
        print(f"  ⚠ rule '{v['name']}' ({v['severity']}): {v['detail']}")


# Writes go through Tools so the CLI, the MCP server and the Telegram bot behave
# identically: rule checks, auto-linking, signal context, seeded rules.
def cmd_add(db: JournalDB, args, tools: Tools) -> int:
    out = tools.add_trade_line(" ".join(args.line), signal_id=args.signal)
    t = out["trade"]
    print(f"added trade #{t['id']}: {t['symbol']} {t['tf'] or ''} {t['direction']} @ {_f(t['entry_price'], 4)} "
          f"sl {_f(t['sl_price'], 4)} tp {_f(t['tp_price'], 4)}  planned R:R={_f(t['planned_rr'])}  "
          f"tags={t['entry_tags']}"
          + (f"  linked signal #{out['auto_linked_signal']}" if out["auto_linked_signal"] else ""))
    _print_violations(out["rule_violations"])
    return 0


def cmd_close(db: JournalDB, args, tools: Tools) -> int:
    out = tools.close_trade_line(" ".join(args.line))
    t = out["trade"]
    print(f"closed trade #{t['id']}: {t['outcome']} R={_f(t['r_multiple'])} pnl={_f(t['pnl_amount'])} "
          f"exit_tags={t['exit_tags']}")
    if out.get("outcome_note"):
        print(f"  ⚠ {out['outcome_note']}")
    _print_violations(out["rule_violations"])
    return 0


def cmd_skip(db: JournalDB, args, tools: Tools) -> int:
    t = tools.skip_signal(args.signal_id, reason=" ".join(args.reason) or None, tags=args.tag)["trade"]
    print(f"recorded skip #{t['id']} on signal #{args.signal_id} ({t['symbol']} {t['tf']} {t['direction']})")
    return 0


def cmd_event(db: JournalDB, args, tools: Tools) -> int:
    data = {}
    for kv in args.data:
        k, _, v = kv.partition("=")
        try:
            data[k] = float(v)
        except ValueError:
            data[k] = v
    out = tools.add_event(args.trade_id, args.type, data)
    e = out["event"]
    print(f"event #{e['id']} on trade #{e['trade_id']}: {e['type']} {e['data']}")
    _print_violations(out["rule_violations"])
    return 0


def cmd_tag(db: JournalDB, args, tools: Tools) -> int:
    if args.tag_cmd == "list":
        tags = db.list_tags(args.category)
        print(_table([[t.category, t.name] for t in tags], ["category", "name"]))
    elif args.tag_cmd == "add":
        t = db.get_or_create_tag(args.name, args.category)
        print(f"tag '{t.name}' ({t.category})")
    elif args.tag_cmd == "rename":
        t = db.rename_tag(args.old, args.new)
        print(f"renamed → '{t.name}'")
    elif args.tag_cmd == "category":
        t = db.set_tag_category(args.name, args.category)
        print(f"'{t.name}' → {t.category}")
    elif args.tag_cmd == "attach":
        db.attach_tags(args.trade_id, args.names, args.phase)
        print(f"attached {args.names} ({args.phase}) to #{args.trade_id}")
    elif args.tag_cmd == "detach":
        db.detach_tag(args.trade_id, args.name)
        print(f"detached '{args.name}' from #{args.trade_id}")
    return 0


def _filtered(db: JournalDB, args) -> list[Trade]:
    return db.list_trades(
        symbol=getattr(args, "symbol", None), tf=getattr(args, "tf", None),
        direction=getattr(args, "direction", None), status=getattr(args, "status", None),
        outcome=getattr(args, "outcome", None), tags=getattr(args, "tag", None) or None,
        since=analytics.period_to_since(getattr(args, "period", None)),
        limit=getattr(args, "limit", None),
    )


def cmd_list(db: JournalDB, args, tools: Tools) -> int:
    trades = _filtered(db, args)
    if not trades:
        print("no trades")
        return 0
    print(_table([_trade_line(t) for t in trades], _TRADE_HEADER))
    return 0


def cmd_show(db: JournalDB, args, tools: Tools) -> int:
    t = db.get_trade(args.trade_id)
    if t is None:
        print(f"no trade #{args.trade_id}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(t.to_dict(), ensure_ascii=False, indent=2))
        return 0
    rr = analytics.planned_rr(t.direction, t.entry_price, t.sl_price, t.tp_price)
    print(f"trade #{t.id}  {t.status}  {t.symbol} {t.tf or '-'} {t.direction}")
    print(f"  entry {_f(t.entry_price, 4)}  sl {_f(t.sl_price, 4)}  tp {_f(t.tp_price, 4)}  "
          f"exit {_f(t.exit_price, 4)}  planned R:R {_f(rr)}")
    print(f"  size {_f(t.position_size, 4)}  risk {_f(t.risk_amount)} ({_f(t.risk_pct)}%)  "
          f"lev {_f(t.leverage)}  fees {_f(t.fees)}  conf {t.confidence or '-'}")
    print(f"  outcome {t.outcome or '-'}  R {_f(t.r_multiple)}  pnl {_f(t.pnl_amount)}")
    print(f"  opened {_ts(t.opened_ts)}  closed {_ts(t.closed_ts)}  session {t.ctx_session or '-'}")
    print(f"  entry tags: {t.entry_tags}\n  exit tags:  {t.exit_tags}")
    if t.entry_reason:
        print(f"  entry reason: {t.entry_reason}")
    if t.exit_reason:
        print(f"  exit reason:  {t.exit_reason}")
    if t.notes:
        print(f"  notes: {t.notes}")
    if t.signal:
        s = t.signal
        print(f"  signal #{s.id}: {s.event} {s.side} line {s.line_price} touches={s.touches} "
              f"rsi={s.rsi} vol={s.vol_ratio} atr_dist={s.atr_dist} @ {_ts(s.candle_ts)}")
    for e in t.events:
        print(f"  event {_ts(e.event_ts)} {e.type} {e.data}")
    for sc in t.screenshots:
        print(f"  screenshot {sc.phase}: {sc.path}")
    return 0


def cmd_signals(db: JournalDB, args, tools: Tools) -> int:
    sigs = db.list_signals(symbol=args.symbol, tf=args.tf, source=args.source, limit=args.limit)
    if not sigs:
        print("no signals")
        return 0
    rows = [[f"#{s.id}", s.source, s.symbol, s.tf, s.event, s.side, _f(s.price, 4),
             _f(s.line_price, 4), s.touches, _f(s.rsi, 1), _f(s.vol_ratio), _ts(s.candle_ts)]
            for s in sigs]
    print(_table(rows, ["id", "src", "symbol", "tf", "event", "side", "price", "line",
                        "touch", "rsi", "vol", "candle (UTC)"]))
    return 0


def import_signals_csv(db: JournalDB, path: str, source: str = "backtest",
                       symbol: str | None = None) -> tuple[int, int]:
    """Load a replay CSV (columns as ``Signal.to_dict()``) into ``signals``.
    Rows lack ``line_id``, so one is synthesised from side + line price.
    Returns (inserted, rows); idempotent on re-import."""
    before = db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    n = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n += 1
            d = {k: (v if v != "" else None) for k, v in row.items()}
            for k in ("price", "line", "atr_dist", "vol_ratio", "rsi"):
                if d.get(k) is not None:
                    d[k] = float(d[k])
            for k in ("touches", "age_bars"):
                if d.get(k) is not None:
                    d[k] = int(float(d[k]))
            if symbol:
                d["symbol"] = symbol
            d.setdefault("exchange", "OKX")
            d["line_id"] = d.get("line_id") or f"csv:{d['side']}:{d['line']:.6g}"
            db.insert_signal(d, source=source)
    after = db.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    return after - before, n


def cmd_import_signals(db: JournalDB, args, tools: Tools) -> int:
    inserted, n = import_signals_csv(db, args.csv, source=args.source, symbol=args.symbol)
    print(f"imported {inserted} new of {n} rows from {args.csv} (source={args.source})")
    return 0


def cmd_footer(db: JournalDB, args, tools: Tools) -> int:
    """Preview the alert footer for a stored signal."""
    from .footer import alert_footer
    sig = db.get_signal(args.signal_id)
    if sig is None:
        print(f"no signal #{args.signal_id}", file=sys.stderr)
        return 1
    d = sig.to_dict()
    d["line"] = d["line_price"]
    print(alert_footer(db, d, sig.id))
    return 0


def _coach(tools: Tools):
    from .coach import Coach
    cfg = tools.cfg.ai if tools.cfg else None
    return Coach(tools, cfg)


def cmd_ask(db: JournalDB, args, tools: Tools) -> int:
    import asyncio
    from .coach import AskBudgetExceeded, CoachError
    coach = _coach(tools)
    try:
        ans = asyncio.run(coach.ask(" ".join(args.question), store=not args.no_store))
    except (AskBudgetExceeded, CoachError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(ans.text)
    print(f"\n[{len(ans.tool_calls)} tool calls: {', '.join(c.name for c in ans.tool_calls) or '-'} · "
          f"{ans.model} · {ans.prompt_version} · usage {ans.usage}]")
    if ans.unverified_numbers:
        print(f"⚠ numbers not found in tool results: {', '.join(ans.unverified_numbers)}")
    return 0


def cmd_review(db: JournalDB, args, tools: Tools) -> int:
    import asyncio
    from .coach import CoachError, format_review
    try:
        r = asyncio.run(_coach(tools).review(args.trade_id, store=not args.no_store,
                                             with_images=not args.no_images))
    except CoachError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else format_review(r))
    return 0 if "error" not in r else 1


def cmd_report(db: JournalDB, args, tools: Tools) -> int:
    import asyncio
    from . import report as R
    from .coach import CoachError
    coach = _coach(tools) if args.narrative else None
    try:
        out = asyncio.run(R.generate(db, args.kind, coach, store=not args.dry_run))
    except CoachError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(out["metrics"], ensure_ascii=False, indent=2))
    else:
        print(out["markdown"])
    if args.png:
        png = R.chart_png(out["metrics"])
        if png is None:
            print("(no chart: matplotlib missing or nothing to draw)", file=sys.stderr)
        else:
            Path(args.png).write_bytes(png)
            print(f"wrote {args.png}", file=sys.stderr)
    if not args.dry_run:
        print(f"(stored as ai_analysis #{out['analysis_id']})", file=sys.stderr)
    return 0


def cmd_backup(db: JournalDB, args, tools: Tools) -> int:
    from . import backup as B
    dest = args.dir or (tools.cfg.journal.backup_dir if tools.cfg else None) or "data/backups"
    keep = args.keep if args.keep is not None else (tools.cfg.journal.backup_keep if tools.cfg else 14)
    if args.verify:
        print(json.dumps(B.verify(args.verify), indent=2))
        return 0
    out = B.backup(db, dest, keep)
    print(f"wrote {out['db']} ({out['bytes']} bytes) and {out['md']}")
    for p in out["pruned"]:
        print(f"pruned {p}")
    v = B.verify(out["db"])
    print(f"verified: integrity={v['integrity']} trades={v['trades']} signals={v['signals']}")
    return 0 if v["integrity"] == "ok" else 1


def cmd_memories(db: JournalDB, args, tools: Tools) -> int:
    from .memory import format_memories
    if args.mem_cmd == "list":
        print(format_memories(tools.memories(refresh=not args.no_refresh)))
    elif args.mem_cmd == "confirm":
        m = tools.confirm_memory(args.memory_id)
        print(f"confirmed #{m['id']}: {m['content']}")
    elif args.mem_cmd == "forget":
        tools.forget_memory(args.memory_id)
        print(f"forgot #{args.memory_id}")
    elif args.mem_cmd == "note":
        m = tools.add_memory_note(" ".join(args.text), args.type)
        print(f"noted #{m['id']}: {m['content']}")
    return 0


def cmd_stats(db: JournalDB, args, tools: Tools) -> int:
    trades = _filtered(db, args)
    label = f"period={args.period or 'all'}" + (f" symbol={args.symbol}" if args.symbol else "") \
        + (f" tf={args.tf}" if args.tf else "")
    if args.json:
        out = {"filters": label, "summary": analytics.summarize(trades)}
        if args.by in ("tags", "all"):
            out["tags"] = analytics.tag_stats(trades)
        if args.by in ("features", "all"):
            out["features"] = analytics.feature_stats(trades)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(_summary_block(f"Summary ({label})", analytics.summarize(trades)))
    if args.by in ("tags", "all"):
        print("\nBy tag (entry):")
        print(_group_table(analytics.tag_stats(trades, "ENTRY")) or "  (none)")
        print("\nBy tag (exit):")
        print(_group_table(analytics.tag_stats(trades, "EXIT")) or "  (none)")
    if args.by in ("features", "all"):
        for name, groups in analytics.feature_stats(trades).items():
            if groups:
                print(f"\nBy {name}:")
                print(_group_table(groups))
    return 0


def cmd_export(db: JournalDB, args, tools: Tools) -> int:
    from . import export as X
    trades = _filtered(db, args)
    if args.format == "json":
        text = X.to_json(list(reversed(trades)))
    elif args.format == "csv":
        text = X.to_csv(list(reversed(trades)))
    else:
        text = X.to_markdown(trades)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


# ── argparse ─────────────────────────────────────────────────────────────────
def _add_filters(p: argparse.ArgumentParser, with_limit: bool = True) -> None:
    p.add_argument("--symbol")
    p.add_argument("--tf")
    p.add_argument("--direction", choices=["LONG", "SHORT"])
    p.add_argument("--status", choices=["OPEN", "CLOSED", "SKIPPED"])
    p.add_argument("--outcome", choices=["WIN", "LOSS", "BE"])
    p.add_argument("--tag", action="append", help="require this tag (repeatable)")
    p.add_argument("--period", help="30d, 12w, 6m, 1y, all (default all)")
    if with_limit:
        p.add_argument("--limit", type=int)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m break_signal.journal", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help=f"journal sqlite path (default: config journal.db or {DEFAULT_DB})")
    ap.add_argument("-c", "--config", default="config.yaml", help="config.yaml (optional)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="log a new trade from a one-line description")
    p.add_argument("line", nargs="+")
    p.add_argument("--signal", type=int, help="link to a signal id")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("close", help="close: <id> <exit> [win|loss|be] [#tags] [reason]")
    p.add_argument("line", nargs="+")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("skip", help="record a deliberate pass on a signal")
    p.add_argument("signal_id", type=int)
    p.add_argument("reason", nargs="*")
    p.add_argument("--tag", action="append")
    p.set_defaults(fn=cmd_skip)

    p = sub.add_parser("event", help="log something that happened during a trade")
    p.add_argument("trade_id", type=int)
    p.add_argument("type", help="sl_moved | tp_moved | partial_close | added | note")
    p.add_argument("data", nargs="*", help="key=value pairs, e.g. from=225 to=222")
    p.set_defaults(fn=cmd_event)

    p = sub.add_parser("tag", help="manage the word bank")
    ts = p.add_subparsers(dest="tag_cmd", required=True)
    q = ts.add_parser("list"); q.add_argument("--category", choices=TAG_CATEGORIES)
    q = ts.add_parser("add"); q.add_argument("name"); q.add_argument("--category", default="OTHER", choices=TAG_CATEGORIES)
    q = ts.add_parser("rename"); q.add_argument("old"); q.add_argument("new")
    q = ts.add_parser("category"); q.add_argument("name"); q.add_argument("category", choices=TAG_CATEGORIES)
    q = ts.add_parser("attach"); q.add_argument("trade_id", type=int); q.add_argument("phase", choices=["ENTRY", "EXIT"]); q.add_argument("names", nargs="+")
    q = ts.add_parser("detach"); q.add_argument("trade_id", type=int); q.add_argument("name")
    p.set_defaults(fn=cmd_tag)

    p = sub.add_parser("list", help="list trades")
    _add_filters(p)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show one trade in full")
    p.add_argument("trade_id", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("signals", help="list stored breakout signals")
    p.add_argument("--symbol"); p.add_argument("--tf"); p.add_argument("--source")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_signals)

    p = sub.add_parser("import-signals", help="load a replay CSV into the signals table")
    p.add_argument("csv")
    p.add_argument("--source", default="backtest", choices=["backtest", "live", "pine"])
    p.add_argument("--symbol", help="override the symbol column (e.g. SOL-USDT-SWAP)")
    p.set_defaults(fn=cmd_import_signals)

    p = sub.add_parser("footer", help="preview the alert history footer for a signal id")
    p.add_argument("signal_id", type=int)
    p.set_defaults(fn=cmd_footer)

    p = sub.add_parser("ask", help="ask the AI coach (needs GEMINI_API_KEY or ANTHROPIC_API_KEY)")
    p.add_argument("question", nargs="+")
    p.add_argument("--no-store", action="store_true", help="don't record in ai_analysis")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("review", help="AI post-trade review of one trade (needs GEMINI_API_KEY or ANTHROPIC_API_KEY)")
    p.add_argument("trade_id", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-store", action="store_true")
    p.add_argument("--no-images", action="store_true", help="don't attach the trade's screenshots")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("report", help="weekly/monthly review (metrics; --narrative adds AI notes)")
    p.add_argument("--kind", choices=["weekly", "monthly"], default="weekly")
    p.add_argument("--narrative", action="store_true", help="add coach notes (needs GEMINI_API_KEY or ANTHROPIC_API_KEY)")
    p.add_argument("--dry-run", action="store_true", help="print only; don't store in ai_analysis")
    p.add_argument("--png", help="also write the equity/tag chart to this path (needs matplotlib)")
    p.add_argument("--json", action="store_true", help="print the metrics block instead of markdown")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("backup", help="snapshot the journal (.db + .md) into the backup dir")
    p.add_argument("--dir", help="destination (default: config journal.backup_dir or data/backups)")
    p.add_argument("--keep", type=int, help="snapshots to retain (default: config or 14)")
    p.add_argument("--verify", metavar="SNAPSHOT.db", help="instead: integrity-check an existing snapshot")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("memories", help="coach memory: evidence-backed observations")
    ms = p.add_subparsers(dest="mem_cmd", required=True)
    q = ms.add_parser("list"); q.add_argument("--no-refresh", action="store_true")
    q = ms.add_parser("confirm"); q.add_argument("memory_id", type=int)
    q = ms.add_parser("forget"); q.add_argument("memory_id", type=int)
    q = ms.add_parser("note"); q.add_argument("text", nargs="+"); q.add_argument("--type", default="preference", choices=["preference", "terminology"])
    p.set_defaults(fn=cmd_memories)

    p = sub.add_parser("stats", help="deterministic performance numbers")
    _add_filters(p, with_limit=False)
    p.add_argument("--by", choices=["summary", "tags", "features", "all"], default="all")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("export", help="dump the journal")
    _add_filters(p, with_limit=False)
    p.add_argument("--format", choices=["md", "csv", "json"], default="md")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_export)
    return ap


def _open_tools(args) -> Tools:
    """Resolve config (optional) → JournalDB → Tools, the same surface the MCP server uses."""
    from ..config import Config, load_config

    db_path = args.db
    cfg: Config | None = None
    account_size = None
    cfg_path = Path(args.config)
    if cfg_path.exists():
        cfg = load_config(cfg_path)
        account_size = cfg.journal.account_size
        db_path = db_path or cfg.journal.db
    return Tools(JournalDB(db_path or DEFAULT_DB, account_size=account_size), cfg)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tools = _open_tools(args)
    try:
        return args.fn(tools.db, args, tools)
    except (ParseError, ValueError, KeyError, sqlite3.IntegrityError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"error: {msg}", file=sys.stderr)
        return 2
    finally:
        tools.db.close()


if __name__ == "__main__":
    sys.exit(main())
