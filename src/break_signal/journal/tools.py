"""The shared tool surface: one set of functions, three front-ends.

``mcp_server.py`` exposes these to Claude Code, ``coach.py`` (phase J3) to the
Anthropic SDK tool runner, ``cli.py`` to the shell. Every method returns a
JSON-serialisable dict/list. Each docstring says whether the result is a
FACT (stored / live data), a CALC (deterministic number from ``analytics``)
or a WRITE.
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

import numpy as np

from ..config import Config, JournalCfg, bar_seconds
from ..core import indicators
from ..core.engine import Engine
from ..core.params import Params
from ..core.types import Candles, ms_to_iso
from . import analytics, memory, rules, similar
from .db import JournalDB, now_ms
from .models import Trade
from .parser import parse_close, parse_trade, resolve_symbol

AUTO_LINK_BARS = 3   # a new trade links to a matching alert within this many bars


def json_safe(x: Any) -> Any:
    """Recursively convert numpy scalars so the result is strict-JSON-safe. NaN → null;
    ±inf → the string "inf"/"-inf" (an all-winning profit factor is real
    information, not missing data; and browsers reject bare ``Infinity``)."""
    return _json(x)


def _json(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _json(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json(v) for v in x]
    if isinstance(x, (np.floating, float)):
        f = float(x)
        if math.isnan(f):
            return None
        if math.isinf(f):
            return "inf" if f > 0 else "-inf"
        return f
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def _iso(ms: int | None) -> str | None:
    return ms_to_iso(ms) if ms is not None else None


def trade_dict(t: Trade, full: bool = False) -> dict:
    d = {
        "id": t.id, "status": t.status, "symbol": t.symbol, "tf": t.tf, "direction": t.direction,
        "signal_id": t.signal_id,
        "entry_price": t.entry_price, "sl_price": t.sl_price, "tp_price": t.tp_price,
        "exit_price": t.exit_price,
        "planned_rr": analytics.planned_rr(t.direction, t.entry_price, t.sl_price, t.tp_price),
        "outcome": t.outcome, "r_multiple": t.r_multiple, "pnl_amount": t.pnl_amount,
        "opened": _iso(t.opened_ts), "closed": _iso(t.closed_ts),
        "entry_tags": t.entry_tags, "exit_tags": t.exit_tags,
        "entry_reason": t.entry_reason, "exit_reason": t.exit_reason,
        "notes": t.notes,
        "ctx_rsi": t.ctx_rsi, "ctx_atr_dist": t.ctx_atr_dist, "ctx_session": t.ctx_session,
    }
    if full:
        d.update({
            "position_size": t.position_size, "leverage": t.leverage, "fees": t.fees,
            "risk_amount": t.risk_amount, "risk_pct": t.risk_pct, "confidence": t.confidence,
            "emotion_before": t.emotion_before, "emotion_after": t.emotion_after,
            "ctx_atr": t.ctx_atr, "ctx_vol_ratio": t.ctx_vol_ratio,
            "opened_ts": t.opened_ts, "closed_ts": t.closed_ts,
            "events": [{"id": e.id, "type": e.type, "data": e.data, "ts": _iso(e.event_ts)} for e in t.events],
            "screenshots": [{"phase": s.phase, "path": s.path} for s in t.screenshots],
            "signal": t.signal.to_dict() if t.signal else None,
        })
    return _json(d)


# ── market snapshot (pure part, testable without network) ───────────────────
def snapshot_from_candles(symbol: str, tf: str, candles: Candles, params: Params,
                          broken_ids: set[str] | None = None) -> dict:
    """FACT (candles) + CALC (Engine): lines, ATR, RSI, distances, last-bar signal."""
    n = len(candles)
    if n < 30:
        return {"symbol": symbol, "tf": tf, "error": f"only {n} candles"}
    engine = Engine(params, bar_seconds(tf), symbol, "OKX", tf)
    res = engine.evaluate(candles, broken_ids or set())
    last = n - 1
    price = float(candles.close[last])
    atr = res.atr_last
    rsi_arr = indicators.rsi(candles.close, 14)
    vol_sma = indicators.sma(candles.volume, 20)
    rsi = float(rsi_arr[last])
    vol_ratio = float(candles.volume[last] / vol_sma[last]) if vol_sma[last] > 0 else None
    lines = []
    for ln in res.lines:
        value = ln.value_at(last)
        lines.append({
            "side": "resistance" if ln.side == "R" else "support",
            "value": round(value, 6),
            "dist_atr": round((value - price) / atr, 2) if atr else None,
            "dist_pct": round((value - price) / price * 100, 2),
            "touches": ln.touches,
            "age_bars": last - ln.bx,
            "span_bars": ln.bx - ln.ax,
            "slope_per_bar": round(ln.slope, 6),
            "line_id": ln.id,
            # two points to draw it: anchor A → current bar
            "anchor_ts": int(ln.ts_a), "anchor_value": round(ln.ay, 6),
            "last_ts": int(candles.ts[last]),
        })
    lines.sort(key=lambda d: abs(d["dist_pct"]))
    res_above = [d for d in lines if d["side"] == "resistance" and d["value"] > price]
    sup_below = [d for d in lines if d["side"] == "support" and d["value"] < price]
    chg = (price / float(candles.close[last - 1]) - 1) * 100 if last >= 1 else None
    return _json({
        "symbol": symbol, "tf": tf, "as_of": _iso(int(candles.ts[last])), "candles": n,
        "price": price, "change_pct_last_bar": chg,
        "atr": atr, "atr_pct": atr / price * 100 if atr else None,
        "rsi": rsi, "rsi_band": analytics.rsi_band(rsi),
        "vol_ratio": vol_ratio,
        "lines": lines,
        "nearest_resistance_above": res_above[0] if res_above else None,
        "nearest_support_below": sup_below[0] if sup_below else None,
        "last_bar_signal": res.signals[0].to_dict() if res.signals else None,
        "note": "lines/ATR/RSI computed by the Break Signal engine on confirmed candles only",
    })


class Tools:
    def __init__(self, db: JournalDB, cfg: Config | None = None):
        self.db = db
        self.cfg = cfg
        jc = cfg.journal if cfg else JournalCfg()
        self.aliases = jc.symbol_aliases
        self.params = cfg.to_params() if cfg else Params()
        self.state_db = cfg.state_db if cfg else None
        rules.ensure_seed(db)

    def _sym(self, s: str) -> str:
        return resolve_symbol(s, self.aliases)

    def _trades(self, period: str | None = None, **filters) -> list[Trade]:
        return self.db.list_trades(since=analytics.period_to_since(period), **filters)

    # ── FACT ────────────────────────────────────────────────────────────
    def recent_signals(self, symbol: str | None = None, tf: str | None = None,
                       source: str | None = None, limit: int = 20) -> list[dict]:
        """FACT: breakout alerts the watcher/backtest stored, newest first."""
        sigs = self.db.list_signals(symbol=self._sym(symbol) if symbol else None, tf=tf,
                                    source=source, limit=limit)
        return [_json({**s.to_dict(), "candle": _iso(s.candle_ts)}) for s in sigs]

    def search_trades(self, symbol: str | None = None, tf: str | None = None,
                      direction: str | None = None, status: str | None = None,
                      outcome: str | None = None, tags: list[str] | None = None,
                      period: str | None = None, signal_linked: bool | None = None,
                      limit: int = 50) -> dict:
        """FACT: trade rows matching the filters (all tags required), newest first."""
        ts = self._trades(period, symbol=self._sym(symbol) if symbol else None, tf=tf,
                          direction=direction.upper() if direction else None, status=status,
                          outcome=outcome.upper() if outcome else None, tags=tags,
                          signal_linked=signal_linked, limit=limit)
        return {"n": len(ts), "trades": [trade_dict(t) for t in ts]}

    def get_trade(self, trade_id: int) -> dict:
        """FACT: one trade in full — events, screenshots, linked signal, stored rule violations."""
        t = self.db.get_trade(trade_id)
        if t is None:
            return {"error": f"no trade #{trade_id}"}
        d = trade_dict(t, full=True)
        d["rule_violations"] = rules.stored_violations(self.db, trade_id)
        return d

    def list_tags(self) -> list[dict]:
        """FACT: the tag word bank."""
        return [t.to_dict() for t in self.db.list_tags()]

    def list_rules(self) -> list[dict]:
        """FACT: the trader's structured rules."""
        return rules.list_rules(self.db)

    # ── CALC ────────────────────────────────────────────────────────────
    def stats(self, period: str = "all", symbol: str | None = None, tf: str | None = None,
              direction: str | None = None, tags: list[str] | None = None) -> dict:
        """CALC: win rate, PF, expectancy (avg R), drawdown, streaks. Always carries n."""
        ts = self._trades(period, symbol=self._sym(symbol) if symbol else None, tf=tf,
                          direction=direction.upper() if direction else None, tags=tags)
        return _json({"period": period, "filters": {"symbol": symbol, "tf": tf, "direction": direction,
                                                    "tags": tags}, **analytics.summarize(ts)})

    def tag_stats(self, period: str = "all", phase: str | None = None) -> dict:
        """CALC: per-tag performance. phase = ENTRY | EXIT | null (both)."""
        return _json({"period": period, "phase": phase,
                      "tags": analytics.tag_stats(self._trades(period), phase)})

    def feature_stats(self, period: str = "all") -> dict:
        """CALC: performance by tf, direction, session, RSI band, ATR band, signal side/event."""
        return _json({"period": period, **analytics.feature_stats(self._trades(period))})

    def signal_history(self, tf: str, event: str, side: str | None = None,
                       symbol: str | None = None, period: str | None = None) -> dict:
        """CALC: how the trader has done on this kind of alert (tf + break direction),
        with best/worst entry tag. This is what the alert footer shows."""
        if side is None:
            side = "resistance" if event == "break_up" else "support"
        return _json(analytics.signal_history(self._trades(period), tf=tf, event=event, side=side,
                                              symbol=self._sym(symbol) if symbol else None))

    def equity_curve(self, period: str = "all") -> list[dict]:
        """CALC: cumulative R after each closed trade."""
        return _json(analytics.equity_curve(self._trades(period)))

    def similar_trades(self, symbol: str, direction: str, tf: str | None = None,
                       side: str | None = None, event: str | None = None,
                       tags: list[str] | None = None, rsi: float | None = None,
                       atr_dist: float | None = None, k: int = 8,
                       period: str | None = None) -> dict:
        """CALC: the k most similar past trades (deterministic feature match) + their aggregate."""
        p = similar.Proposed(symbol=self._sym(symbol), direction=direction, tf=tf, side=side,
                             event=event, tags=tags, rsi=rsi, atr_dist=atr_dist)
        return _json(similar.similar_trades(p, self._trades(period), k))

    def rule_check(self, proposed: dict) -> dict:
        """CALC: which of the trader's rules a proposed trade would break.
        ``proposed`` keys: symbol, direction, tf, entry_price, sl_price, tp_price,
        risk_pct, tags, ctx_rsi ..."""
        return _json(rules.check(self.db, proposed))

    def review_context(self, trade_id: int) -> dict:
        """FACT+CALC: everything a post-trade review needs — the trade, rule check,
        stats on the same setup (symbol+tf+direction), and its similar trades."""
        t = self.db.get_trade(trade_id)
        if t is None:
            return {"error": f"no trade #{trade_id}"}
        same_setup = [x for x in self.db.list_trades(symbol=t.symbol, tf=t.tf, direction=t.direction)
                      if x.id != t.id]
        p = similar.Proposed(symbol=t.symbol, direction=t.direction, tf=t.tf,
                             side=t.signal.side if t.signal else None, tags=t.entry_tags,
                             rsi=t.ctx_rsi, atr_dist=t.ctx_atr_dist)
        others = [x for x in self.db.list_trades() if x.id != t.id]
        return _json({
            "trade": trade_dict(t, full=True),
            "rules": rules.check(self.db, t),
            "same_setup_stats": analytics.summarize(same_setup),
            "similar": similar.similar_trades(p, others, 5),
            "tag_stats_entry": {k: v for k, v in analytics.tag_stats(self.db.list_trades(), "ENTRY").items()
                                if k in t.entry_tags},
        })

    # ── memory & reports ────────────────────────────────────────────────
    def memories(self, refresh: bool = False) -> list[dict]:
        """FACT: evidence-backed coach memories (patterns with n ≥ 5, rules broken ≥ 3×).
        ``refresh`` re-derives them from the journal first."""
        if refresh:
            memory.refresh(self.db)
        return _json(memory.list_memories(self.db))

    def refresh_memories(self) -> dict:
        """CALC+WRITE: re-derive memories from the journal (upsert; unconfirmed stale ones removed)."""
        return memory.refresh(self.db)

    def confirm_memory(self, memory_id: int, confirmed: bool = True) -> dict:
        """WRITE: the trader confirms (or un-confirms) an observation."""
        return _json(memory.confirm(self.db, memory_id, confirmed))

    def forget_memory(self, memory_id: int) -> dict:
        """WRITE: delete a memory."""
        memory.forget(self.db, memory_id)
        return {"deleted": memory_id}

    def add_memory_note(self, content: str, type: str = "preference") -> dict:
        """WRITE: a memory the trader states themselves (preference | terminology)."""
        return _json(memory.add_note(self.db, content, type))

    def report(self, kind: str = "weekly") -> dict:
        """CALC: the weekly|monthly metrics block and its markdown (no LLM narrative)."""
        from . import report as R
        m = R.build_metrics(self.db, kind)
        return _json({"kind": kind, "metrics": m, "markdown": R.render(m)})

    # ── market ──────────────────────────────────────────────────────────
    async def market_snapshot(self, symbol: str, tf: str, bars: int = 300) -> dict:
        """FACT (live candles) + CALC (engine): price, ATR, RSI, active lines with
        distances, and whether a breakout fired on the last confirmed bar."""
        import aiohttp
        from ..data import rest as data_rest
        exchange = (self.cfg.exchange if self.cfg else "okx")
        rest = data_rest(exchange)
        symbol = self._sym(symbol)
        try:
            async with aiohttp.ClientSession() as session:
                candles = await rest.fetch_candles(session, symbol, tf, bars)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as e:
            return {"symbol": symbol, "tf": tf, "exchange": exchange,
                    "error": f"{exchange.upper()} fetch failed ({e.__class__.__name__}: {e}). "
                    f"Host: {rest.REST_URL}. If it is blocked here, set `exchange: binance` "
                    f"in config.yaml, or point OKX_REST_URL / BINANCE_REST_URL at another host."}
        if len(candles) == 0:
            return {"symbol": symbol, "tf": tf, "exchange": exchange,
                    "error": f"{exchange.upper()} returned no candles (bad symbol/tf?)"}
        broken: set[str] = set()
        if self.state_db:
            try:
                from ..core.state import State
                st = State(self.state_db)
                broken = st.broken_ids(symbol, tf)
                st.close()
            except Exception:  # noqa: BLE001 — snapshot must not depend on state.db
                broken = set()
        return snapshot_from_candles(symbol, tf, candles, self.params, broken)

    # ── WRITE ───────────────────────────────────────────────────────────
    def add_trade(self, symbol: str, direction: str, entry_price: float | None = None,
                  sl_price: float | None = None, tp_price: float | None = None,
                  tf: str | None = None, tags: list[str] | None = None,
                  entry_reason: str | None = None, position_size: float | None = None,
                  risk_amount: float | None = None, risk_pct: float | None = None,
                  leverage: float | None = None, fees: float | None = None,
                  confidence: int | None = None, emotion_before: str | None = None,
                  notes: str | None = None, signal_id: int | None = None,
                  auto_link: bool = True, ctx_rsi: float | None = None,
                  ctx_atr_dist: float | None = None, ctx_vol_ratio: float | None = None,
                  ctx_atr: float | None = None) -> dict:
        """WRITE: log an OPEN trade. Unspecified fields stay null. If ``signal_id`` is
        omitted and ``auto_link`` is on, links to a matching alert (same symbol/tf,
        direction-consistent event) within the last 3 bars and copies its RSI/ATR context."""
        symbol = self._sym(symbol)
        direction = direction.upper()
        auto_linked: int | None = None
        sig = None
        if signal_id is not None:
            sig = self.db.get_signal(signal_id)
            if sig is None:
                raise KeyError(f"no signal #{signal_id}")
        elif auto_link and tf:
            event = "break_up" if direction == "LONG" else "break_down"
            try:
                since = now_ms() - AUTO_LINK_BARS * bar_seconds(tf) * 1000
            except ValueError:
                since = None            # tf not an OKX bar (e.g. 8H): log the trade, skip linking
            if since is not None:
                sig = self.db.latest_signal(symbol, tf=tf, event=event, since=since)
                if sig:
                    signal_id = auto_linked = sig.id
        if sig:
            # Copy the alert's market context unless the caller supplied it.
            ctx_rsi = ctx_rsi if ctx_rsi is not None else sig.rsi
            ctx_atr_dist = ctx_atr_dist if ctx_atr_dist is not None else sig.atr_dist
            ctx_vol_ratio = ctx_vol_ratio if ctx_vol_ratio is not None else sig.vol_ratio
        t = self.db.add_trade(
            symbol, direction, entry_tags=tags, tf=tf, entry_price=entry_price, sl_price=sl_price,
            tp_price=tp_price, entry_reason=entry_reason, position_size=position_size,
            risk_amount=risk_amount, risk_pct=risk_pct, leverage=leverage, fees=fees,
            confidence=confidence, emotion_before=emotion_before, notes=notes, signal_id=signal_id,
            ctx_rsi=ctx_rsi, ctx_atr_dist=ctx_atr_dist, ctx_vol_ratio=ctx_vol_ratio, ctx_atr=ctx_atr,
        )
        check = rules.check(self.db, t)
        rules.record_violations(self.db, t.id, check)
        return _json({"trade": trade_dict(t), "auto_linked_signal": auto_linked,
                      "rule_violations": check["violations"]})

    def add_trade_line(self, text: str, signal_id: int | None = None) -> dict:
        """WRITE: log a trade from the one-line syntax
        ``<SYM> [<tf>] long|short <entry> [sl x] [tp x] [size x] [risk x[%]] [#tags] [-- reason]``."""
        p = parse_trade(text, self.aliases)
        f = p.db_fields()
        return self.add_trade(p.symbol, p.direction, entry_price=f["entry_price"], sl_price=f["sl_price"],
                              tp_price=f["tp_price"], tf=f["tf"], tags=p.tags, entry_reason=f["entry_reason"],
                              position_size=f["position_size"], risk_amount=f["risk_amount"],
                              risk_pct=f["risk_pct"], leverage=f["leverage"], fees=f["fees"],
                              confidence=f["confidence"], signal_id=signal_id)

    def close_trade(self, trade_id: int, exit_price: float, outcome: str | None = None,
                    exit_reason: str | None = None, tags: list[str] | None = None,
                    fees: float | None = None, emotion_after: str | None = None) -> dict:
        """WRITE: close a trade. R and PnL are computed, never supplied. ``outcome``
        defaults to the sign of R (BE within ±0.1R); the user may override it."""
        t = self.db.close_trade(trade_id, exit_price, outcome=outcome, exit_reason=exit_reason,
                                exit_tags=tags, fees=fees, emotion_after=emotion_after)
        check = rules.check(self.db, t)
        rules.record_violations(self.db, t.id, check)
        out = {"trade": trade_dict(t), "rule_violations": check["violations"]}
        derived = analytics.derive_outcome(t.r_multiple)
        if outcome and derived and t.outcome != derived:
            # The stated outcome wins (partials etc.), but a contradiction is worth a look.
            out["outcome_note"] = (f"declared {t.outcome} but R is {t.r_multiple:+.2f} (derived {derived}); "
                                   f"stats count it as {t.outcome}")
        return _json(out)

    def close_trade_line(self, text: str) -> dict:
        """WRITE: close from ``<id> <exit> [win|loss|be] [#tags] [reason]``."""
        p = parse_close(text)
        return self.close_trade(p.trade_id, p.exit_price, outcome=p.outcome, exit_reason=p.reason,
                                tags=p.tags)

    def skip_signal(self, signal_id: int, reason: str | None = None,
                    tags: list[str] | None = None) -> dict:
        """WRITE: record a deliberate pass on an alert (status SKIPPED)."""
        return {"trade": trade_dict(self.db.skip_signal(signal_id, reason, tags))}

    def add_event(self, trade_id: int, type: str, data: dict | None = None) -> dict:
        """WRITE: something that happened mid-trade: sl_moved | tp_moved | partial_close | added | note.
        ``data`` e.g. {"from": 225, "to": 222}."""
        e = self.db.add_event(trade_id, type, data)
        t = self.db.get_trade(trade_id)
        check = rules.check(self.db, t) if t else {"violations": []}
        if t:
            rules.record_violations(self.db, t.id, check)
        return _json({"event": e.to_dict(), "rule_violations": check["violations"]})

    def add_tag(self, name: str, category: str = "OTHER") -> dict:
        """WRITE: add a word-bank tag (SETUP | PSYCH | EXIT | MISTAKE | OTHER)."""
        return self.db.get_or_create_tag(name, category).to_dict()

    def tag_trade(self, trade_id: int, tags: list[str], phase: str = "ENTRY") -> dict:
        """WRITE: attach tags to an existing trade (phase ENTRY | EXIT)."""
        self.db.attach_tags(trade_id, tags, phase)
        return {"trade": trade_dict(self.db.get_trade(trade_id))}  # type: ignore[arg-type]

    def add_screenshot(self, trade_id: int, phase: str, path: str) -> dict:
        """WRITE: register a chart screenshot path for a trade (PRE | POST)."""
        return self.db.add_screenshot(trade_id, phase, path).to_dict()

    def update_trade(self, trade_id: int, **fields: Any) -> dict:
        """WRITE: edit trade fields (notes, confidence, emotion_*, sl/tp plan changes ...).
        Re-checks the rules like every other write path — a risk or a stop stated
        after the trade was logged must still reach them."""
        t = self.db.update_trade(trade_id, **fields)
        check = rules.check(self.db, t)
        rules.record_violations(self.db, t.id, check)
        return _json({"trade": trade_dict(t, full=True), "rule_violations": check["violations"]})

    def add_rule(self, name: str, condition: dict, severity: str = "high") -> dict:
        """WRITE: add a structured rule, e.g. {"field":"risk_pct","op":"<=","value":1}."""
        return rules.add_rule(self.db, name, condition, severity)

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> dict:
        """WRITE: enable/disable a rule."""
        rules.set_rule_enabled(self.db, rule_id, enabled)
        return {"rule_id": rule_id, "enabled": enabled}
