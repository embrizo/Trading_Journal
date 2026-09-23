"""AI coach over the Anthropic API — Telegram ``/ask`` and ``/review``.

Read-only: the coach gets FACT/CALC tools from ``tools.py`` but never a write
tool, so the LLM cannot modify the journal; writes stay on explicit commands.
Every answer is stored in ``ai_analysis`` with model + prompt version + the
exact tool results it was given, so it can be audited or re-run.

``anthropic`` is an optional dependency (``pip install -e .[ai]``); this module
imports it lazily so the rest of the journal works without it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import AiCfg
from . import prompts
from .db import now_ms
from .tools import Tools


@dataclass
class ToolCall:
    name: str
    args: dict
    result: Any


@dataclass
class CoachAnswer:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    prompt_version: str = ""
    usage: dict = field(default_factory=dict)
    unverified_numbers: list[str] = field(default_factory=list)
    analysis_id: int | None = None
    stop_reason: str | None = None


class AskBudgetExceeded(RuntimeError):
    pass


class CoachError(RuntimeError):
    """The model could not be reached (credentials, network, API error).
    Message is safe to show to the trader."""


def _friendly(e: Exception) -> CoachError:
    msg_str = str(e).lower()
    try:
        from google.genai import errors as g_errors
        if isinstance(e, g_errors.ClientError):
            if "401" in str(e) or "api_key_invalid" in msg_str:
                return CoachError("Gemini API key rejected — check GEMINI_API_KEY / ai.api_key in config.yaml")
            if "429" in str(e) or "resource_exhausted" in msg_str:
                return CoachError("Gemini rate limit / quota hit (429) — try again later")
            if "404" in str(e):
                return CoachError(f"Gemini model not found: {e}")
            return CoachError(f"Gemini client error: {e}")
        if isinstance(e, g_errors.APIError):
            return CoachError(f"Gemini API error: {e}")
    except ImportError:
        pass

    try:
        import anthropic
        if isinstance(e, TypeError) and "authentication" in msg_str:
            return CoachError("no Anthropic credentials — set ANTHROPIC_API_KEY (or ai.api_key in config.yaml)")
        if isinstance(e, anthropic.AuthenticationError):
            return CoachError("Anthropic API key rejected (401) — check ANTHROPIC_API_KEY / ai.api_key")
        if isinstance(e, anthropic.RateLimitError):
            return CoachError("Anthropic rate limit hit (429) — try again in a minute")
        if isinstance(e, anthropic.APIConnectionError):
            return CoachError(f"cannot reach the Anthropic API: {e}")
        if isinstance(e, anthropic.APIStatusError):
            return CoachError(f"Anthropic API error {e.status_code}: {e.message}")
    except ImportError:
        pass
    return CoachError(f"coach failed: {e.__class__.__name__}: {e}")


def _is_gemini_rate_limit(e: Exception) -> bool:
    try:
        from google.genai import errors as g_errors
    except ImportError:
        return False
    if not isinstance(e, g_errors.ClientError):
        return False
    msg = str(e).lower()
    return "429" in str(e) or "resource_exhausted" in msg


# ── number-parity guard (deterministic, no LLM) ─────────────────────────────
_NUM_RE = re.compile(r"(?<![\w#])[-+]?\d+(?:[.,]\d+)?%?")


def _numbers_in_results(obj: Any, out: set[str]) -> None:
    """Every numeric value in the tool results, in the spellings a reply might use
    (as-is, rounded to 0/1/2 dp, and ×100 for ratios in [0,1])."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        f = float(obj)
        cands = {f, round(f), round(f, 1), round(f, 2)}
        if 0 <= f <= 1:
            cands |= {round(f * 100), round(f * 100, 1)}
        for c in cands:
            out.add(_norm(c))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in_results(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in_results(v, out)
    elif isinstance(obj, str):
        for m in _NUM_RE.findall(obj):
            out.add(_norm(m))


def _norm(x: Any) -> str:
    s = str(x).replace(",", "").rstrip("%").lstrip("+")
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f == int(f) else repr(round(f, 4))


def parity_check(text: str, tool_calls: list[ToolCall]) -> list[str]:
    """Numbers in ``text`` that do not appear in any tool result. Heuristic guard
    for the "never compute" rule — treat hits as warnings, not proof."""
    seen: set[str] = set()
    for tc in tool_calls:
        _numbers_in_results(tc.result, seen)
        _numbers_in_results(tc.args, seen)
    missing = []
    for m in _NUM_RE.findall(text):
        n = _norm(m)
        if n not in seen and n not in missing:
            missing.append(n)
    return missing


# ── the coach ───────────────────────────────────────────────────────────────
class Coach:
    def __init__(self, tools: Tools, cfg: AiCfg | None = None, client: Any = None):
        self.tools = tools
        self.cfg = cfg or AiCfg()
        self._client = client          # injected in tests; built lazily otherwise
        self._calls: list[ToolCall] = []
        if client is None:
            if self.is_gemini:
                try:
                    from google import genai  # noqa: F401
                except ImportError as e:
                    raise ImportError("google-genai is required for Gemini AI coach (pip install google-genai)") from e
            else:
                try:
                    import anthropic  # noqa: F401
                except ImportError as e:
                    raise ImportError("anthropic is required for Claude AI coach (pip install anthropic)") from e

    @property
    def is_gemini(self) -> bool:
        if getattr(self.cfg, "provider", "auto") == "gemini":
            return True
        if getattr(self.cfg, "provider", "auto") == "anthropic":
            return False
        if self._client is not None and (hasattr(self._client, "beta") or hasattr(self._client, "messages")):
            return False
        if "gemini" in (self.cfg.model or "").lower():
            return True
        import os
        if os.environ.get("GEMINI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
            return True
        return False

    async def _call_with_fallback(self, make_call, on_retry=None):
        """Call ``make_call(model)`` with ``cfg.model``; on a Gemini 429, retry once
        with ``cfg.fallback_model`` if one is configured. Returns (response, model_used)."""
        model = self.cfg.model
        try:
            return await make_call(model), model
        except (AskBudgetExceeded, CoachError):
            raise
        except Exception as e:
            fallback = self.cfg.fallback_model
            if not (self.is_gemini and fallback and fallback != model and _is_gemini_rate_limit(e)):
                raise _friendly(e) from e
            if on_retry:
                on_retry()
            try:
                return await make_call(fallback), fallback
            except Exception as e2:
                raise _friendly(e2) from e2

    @property
    def client(self):
        if self._client is None:
            if self.is_gemini:
                import os
                from google import genai
                key = self.cfg.api_key or os.environ.get("GEMINI_API_KEY")
                self._client = genai.Client(api_key=key or None)
            else:
                import os
                import anthropic
                key = self.cfg.api_key or os.environ.get("ANTHROPIC_API_KEY")
                self._client = anthropic.AsyncAnthropic(api_key=key or None)
        return self._client

    # ── read-only tool surface for the tool runner ──────────────────────
    def _record(self, name: str, args: dict, result: Any) -> str:
        self._calls.append(ToolCall(name, args, result))
        return json.dumps(result, ensure_ascii=False)

    def build_tools(self) -> list:
        """Read-only tool wrappers over the ``Tools`` methods."""
        if self.is_gemini:
            return self._build_gemini_tools()
        from anthropic import beta_async_tool
        T = self.tools
        rec = self._record

        @beta_async_tool
        async def market_snapshot(symbol: str, tf: str, bars: int = 300) -> str:
            """FACT (live OKX candles) + CALC (engine): price, ATR, RSI, volume ratio, active
            support/resistance lines with distance in ATR and %, nearest levels, last-bar signal.

            Args:
                symbol: Instrument, alias OK (SOL → SOL-USDT-SWAP).
                tf: Timeframe: 1D, 4H, 1H ...
                bars: Candles to load (default 300).
            """
            return rec("market_snapshot", {"symbol": symbol, "tf": tf, "bars": bars},
                       await T.market_snapshot(symbol, tf, bars))

        @beta_async_tool
        async def journal_stats(period: str = "all", symbol: str | None = None, tf: str | None = None,
                                direction: str | None = None, tags: list[str] | None = None) -> str:
            """CALC: win_rate, profit_factor, avg_r (expectancy), max_drawdown_r, streaks over
            CLOSED trades matching the filters. Every figure comes with n.

            Args:
                period: 30d, 12w, 6m, 1y or all.
                symbol: Instrument filter.
                tf: Timeframe filter.
                direction: LONG or SHORT.
                tags: All of these tags must be present.
            """
            args = dict(period=period, symbol=symbol, tf=tf, direction=direction, tags=tags)
            return rec("journal_stats", args, T.stats(period, symbol, tf, direction, tags))

        @beta_async_tool
        async def journal_tag_stats(period: str = "all", phase: str | None = None) -> str:
            """CALC: per-tag n / wins / losses / win_rate / avg_r / profit_factor.

            Args:
                period: 30d, 6m, 1y or all.
                phase: ENTRY, EXIT or omitted for both.
            """
            return rec("journal_tag_stats", {"period": period, "phase": phase}, T.tag_stats(period, phase))

        @beta_async_tool
        async def journal_feature_stats(period: str = "all") -> str:
            """CALC: performance by tf, direction, session, RSI band, ATR band, signal side/event.

            Args:
                period: 30d, 6m, 1y or all.
            """
            return rec("journal_feature_stats", {"period": period}, T.feature_stats(period))

        @beta_async_tool
        async def journal_signal_history(tf: str, event: str, side: str | None = None,
                                         symbol: str | None = None) -> str:
            """CALC: the trader's record on this kind of alert (tf + break_up/break_down) with
            best/worst entry tag and matched trade ids.

            Args:
                tf: Timeframe of the alert.
                event: break_up (traded LONG) or break_down (traded SHORT).
                side: resistance or support; inferred from event if omitted.
                symbol: Narrow to one instrument.
            """
            args = dict(tf=tf, event=event, side=side, symbol=symbol)
            return rec("journal_signal_history", args, T.signal_history(tf, event, side, symbol))

        @beta_async_tool
        async def journal_similar_trades(symbol: str, direction: str, tf: str | None = None,
                                         side: str | None = None, tags: list[str] | None = None,
                                         rsi: float | None = None, atr_dist: float | None = None,
                                         k: int = 8) -> str:
            """CALC: the k past trades most similar to a proposed setup (deterministic feature
            match) with outcomes, reasons and an aggregate.

            Args:
                symbol: Instrument.
                direction: LONG or SHORT.
                tf: Timeframe.
                side: resistance or support.
                tags: Setup tags of the proposed trade.
                rsi: RSI at the proposed entry.
                atr_dist: Break distance from the line in ATR.
                k: How many matches to return.
            """
            args = dict(symbol=symbol, direction=direction, tf=tf, side=side, tags=tags, rsi=rsi,
                        atr_dist=atr_dist, k=k)
            return rec("journal_similar_trades", args,
                       T.similar_trades(symbol, direction, tf, side, None, tags, rsi, atr_dist, k))

        @beta_async_tool
        async def journal_search_trades(symbol: str | None = None, tf: str | None = None,
                                        direction: str | None = None, status: str | None = None,
                                        outcome: str | None = None, tags: list[str] | None = None,
                                        period: str | None = None, limit: int = 20) -> str:
            """FACT: trades matching the filters, newest first.

            Args:
                symbol: Instrument filter.
                tf: Timeframe filter.
                direction: LONG or SHORT.
                status: OPEN, CLOSED or SKIPPED.
                outcome: WIN, LOSS or BE.
                tags: All of these tags must be present.
                period: 30d, 6m, 1y or all.
                limit: Max rows.
            """
            args = dict(symbol=symbol, tf=tf, direction=direction, status=status, outcome=outcome,
                        tags=tags, period=period, limit=limit)
            return rec("journal_search_trades", args,
                       T.search_trades(symbol, tf, direction, status, outcome, tags, period, None, limit))

        @beta_async_tool
        async def journal_get_trade(trade_id: int) -> str:
            """FACT: one trade in full — plan, outcome, reasons, tags, events, linked signal, violations.

            Args:
                trade_id: The trade id.
            """
            return rec("journal_get_trade", {"trade_id": trade_id}, T.get_trade(trade_id))

        @beta_async_tool
        async def journal_recent_signals(symbol: str | None = None, tf: str | None = None,
                                         limit: int = 10) -> str:
            """FACT: breakout alerts stored by the watcher, newest first.

            Args:
                symbol: Instrument filter.
                tf: Timeframe filter.
                limit: Max rows.
            """
            return rec("journal_recent_signals", {"symbol": symbol, "tf": tf, "limit": limit},
                       T.recent_signals(symbol, tf, None, limit))

        @beta_async_tool
        async def journal_rule_check(proposed: dict) -> str:
            """CALC: which of the trader's rules a proposed trade would violate.

            Args:
                proposed: Keys symbol, direction, tf, entry_price, sl_price, tp_price, risk_pct, tags, ctx_rsi.
            """
            return rec("journal_rule_check", {"proposed": proposed}, T.rule_check(proposed))

        @beta_async_tool
        async def journal_list_rules() -> str:
            """FACT: the trader's structured rules."""
            return rec("journal_list_rules", {}, T.list_rules())

        @beta_async_tool
        async def journal_memories() -> str:
            """FACT: evidence-backed coach memories — patterns with n ≥ 5, rules broken 3+ times,
            and notes the trader confirmed — each with trade ids and period."""
            return rec("journal_memories", {}, T.memories())

        return [market_snapshot, journal_stats, journal_tag_stats, journal_feature_stats,
                journal_signal_history, journal_similar_trades, journal_search_trades,
                journal_get_trade, journal_recent_signals, journal_rule_check, journal_list_rules,
                journal_memories]

    def _build_gemini_tools(self) -> list:
        """Python functions with docstrings and type annotations for Gemini function calling."""
        T = self.tools
        rec = self._record

        async def market_snapshot(symbol: str, tf: str, bars: int = 300) -> str:
            """FACT (live OKX candles) + CALC (engine): price, ATR, RSI, volume ratio, active
            support/resistance lines with distance in ATR and %, nearest levels, last-bar signal.

            Args:
                symbol: Instrument, alias OK (SOL -> SOL-USDT-SWAP).
                tf: Timeframe: 1D, 4H, 1H ...
                bars: Candles to load (default 300).
            """
            return rec("market_snapshot", {"symbol": symbol, "tf": tf, "bars": bars},
                       await T.market_snapshot(symbol, tf, bars))

        async def journal_stats(period: str = "all", symbol: str | None = None, tf: str | None = None,
                                direction: str | None = None, tags: list[str] | None = None) -> str:
            """CALC: win_rate, profit_factor, avg_r (expectancy), max_drawdown_r, streaks over
            CLOSED trades matching the filters. Every figure comes with n.

            Args:
                period: 30d, 12w, 6m, 1y or all.
                symbol: Instrument filter.
                tf: Timeframe filter.
                direction: LONG or SHORT.
                tags: All of these tags must be present.
            """
            args = dict(period=period, symbol=symbol, tf=tf, direction=direction, tags=tags)
            return rec("journal_stats", args, T.stats(period, symbol, tf, direction, tags))

        async def journal_tag_stats(period: str = "all", phase: str | None = None) -> str:
            """CALC: per-tag n / wins / losses / win_rate / avg_r / profit_factor.

            Args:
                period: 30d, 6m, 1y or all.
                phase: ENTRY, EXIT or omitted for both.
            """
            return rec("journal_tag_stats", {"period": period, "phase": phase}, T.tag_stats(period, phase))

        async def journal_feature_stats(period: str = "all") -> str:
            """CALC: performance by tf, direction, session, RSI band, ATR band, signal side/event.

            Args:
                period: 30d, 6m, 1y or all.
            """
            return rec("journal_feature_stats", {"period": period}, T.feature_stats(period))

        async def journal_signal_history(tf: str, event: str, side: str | None = None,
                                         symbol: str | None = None) -> str:
            """CALC: the trader's record on this kind of alert (tf + break_up/break_down) with
            best/worst entry tag and matched trade ids.

            Args:
                tf: Timeframe of the alert.
                event: break_up (traded LONG) or break_down (traded SHORT).
                side: resistance or support; inferred from event if omitted.
                symbol: Narrow to one instrument.
            """
            args = dict(tf=tf, event=event, side=side, symbol=symbol)
            return rec("journal_signal_history", args, T.signal_history(tf, event, side, symbol))

        async def journal_similar_trades(symbol: str, direction: str, tf: str | None = None,
                                         side: str | None = None, tags: list[str] | None = None,
                                         rsi: float | None = None, atr_dist: float | None = None,
                                         k: int = 8) -> str:
            """CALC: the k past trades most similar to a proposed setup (deterministic feature
            match) with outcomes, reasons and an aggregate.

            Args:
                symbol: Instrument.
                direction: LONG or SHORT.
                tf: Timeframe.
                side: resistance or support.
                tags: Setup tags of the proposed trade.
                rsi: RSI at the proposed entry.
                atr_dist: Break distance from the line in ATR.
                k: How many matches to return.
            """
            args = dict(symbol=symbol, direction=direction, tf=tf, side=side, tags=tags, rsi=rsi,
                        atr_dist=atr_dist, k=k)
            return rec("journal_similar_trades", args,
                       T.similar_trades(symbol, direction, tf, side, None, tags, rsi, atr_dist, k))

        async def journal_search_trades(symbol: str | None = None, tf: str | None = None,
                                        direction: str | None = None, status: str | None = None,
                                        outcome: str | None = None, tags: list[str] | None = None,
                                        period: str | None = None, limit: int = 20) -> str:
            """FACT: trades matching the filters, newest first.

            Args:
                symbol: Instrument filter.
                tf: Timeframe filter.
                direction: LONG or SHORT.
                status: OPEN, CLOSED or SKIPPED.
                outcome: WIN, LOSS or BE.
                tags: All of these tags must be present.
                period: 30d, 6m, 1y or all.
                limit: Max rows.
            """
            args = dict(symbol=symbol, tf=tf, direction=direction, status=status, outcome=outcome,
                        tags=tags, period=period, limit=limit)
            return rec("journal_search_trades", args,
                       T.search_trades(symbol, tf, direction, status, outcome, tags, period, None, limit))

        async def journal_get_trade(trade_id: int) -> str:
            """FACT: one trade in full — plan, outcome, reasons, tags, events, linked signal, violations.

            Args:
                trade_id: The trade id.
            """
            return rec("journal_get_trade", {"trade_id": trade_id}, T.get_trade(trade_id))

        async def journal_recent_signals(symbol: str | None = None, tf: str | None = None,
                                         limit: int = 10) -> str:
            """FACT: breakout alerts stored by the watcher, newest first.

            Args:
                symbol: Instrument filter.
                tf: Timeframe filter.
                limit: Max rows.
            """
            return rec("journal_recent_signals", {"symbol": symbol, "tf": tf, "limit": limit},
                       T.recent_signals(symbol, tf, None, limit))

        async def journal_rule_check(proposed: dict) -> str:
            """CALC: which of the trader's rules a proposed trade would violate.

            Args:
                proposed: Keys symbol, direction, tf, entry_price, sl_price, tp_price, risk_pct, tags, ctx_rsi.
            """
            return rec("journal_rule_check", {"proposed": proposed}, T.rule_check(proposed))

        async def journal_list_rules() -> str:
            """FACT: the trader's structured rules."""
            return rec("journal_list_rules", {}, T.list_rules())

        async def journal_memories() -> str:
            """FACT: evidence-backed coach memories — patterns with n >= 5, rules broken 3+ times,
            and notes the trader confirmed — each with trade ids and period."""
            return rec("journal_memories", {}, T.memories())

        return [market_snapshot, journal_stats, journal_tag_stats, journal_feature_stats,
                journal_signal_history, journal_similar_trades, journal_search_trades,
                journal_get_trade, journal_recent_signals, journal_rule_check, journal_list_rules,
                journal_memories]

    # ── budget ──────────────────────────────────────────────────────────
    def asks_today(self) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        row = self.tools.db.conn.execute(
            "SELECT COUNT(*) FROM ai_analysis WHERE kind='suggestion' AND created_ts >= ?",
            (int(start.timestamp() * 1000),)).fetchone()
        return int(row[0])

    def _store(self, kind: str, trade_id: int | None, model: str, version: str,
               input_metrics: Any, output: str) -> int:
        cur = self.tools.db.conn.execute(
            "INSERT INTO ai_analysis(trade_id, kind, model, prompt_version, input_metrics, output, created_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (trade_id, kind, model, version, json.dumps(input_metrics, ensure_ascii=False, default=str),
             output, now_ms()))
        self.tools.db.conn.commit()
        return int(cur.lastrowid)

    # ── /ask ────────────────────────────────────────────────────────────
    async def ask(self, question: str, signal: dict | None = None, store: bool = True) -> CoachAnswer:
        """Answer a trader's question with the read-only tools; stores the answer."""
        if self.is_gemini:
            return await self._ask_gemini(question, signal=signal, store=store)
        if self.asks_today() >= self.cfg.daily_ask_limit:
            raise AskBudgetExceeded(f"daily /ask limit ({self.cfg.daily_ask_limit}) reached")
        self._calls = []
        user = question if signal is None else \
            f"{question}\n\nCURRENT SIGNAL:\n{json.dumps(signal, ensure_ascii=False)}"
        try:
            runner = self.client.beta.messages.tool_runner(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                max_iterations=self.cfg.max_tool_calls,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": prompts.COACH_V1, "cache_control": {"type": "ephemeral"}}],
                tools=self.build_tools(),
                messages=[{"role": "user", "content": user}],
            )
            final = await runner.until_done()
        except (AskBudgetExceeded, CoachError):
            raise
        except Exception as e:  # noqa: BLE001 — SDK/network errors become one readable message
            raise _friendly(e) from e
        text = "".join(b.text for b in final.content if b.type == "text").strip()
        usage = _usage(final)
        ans = CoachAnswer(text=text, tool_calls=list(self._calls), model=self.cfg.model,
                          prompt_version=prompts.COACH_VERSION, usage=usage,
                          stop_reason=getattr(final, "stop_reason", None))
        ans.unverified_numbers = parity_check(text, ans.tool_calls)
        if store:
            ans.analysis_id = self._store(
                "suggestion", None, self.cfg.model, prompts.COACH_VERSION,
                {"question": question, "signal": signal, "usage": usage,
                 "tool_calls": [{"name": c.name, "args": c.args, "result": c.result} for c in ans.tool_calls],
                 "unverified_numbers": ans.unverified_numbers},
                text)
        return ans

    async def _ask_gemini(self, question: str, signal: dict | None = None, store: bool = True) -> CoachAnswer:
        if self.asks_today() >= self.cfg.daily_ask_limit:
            raise AskBudgetExceeded(f"daily /ask limit ({self.cfg.daily_ask_limit}) reached")
        self._calls = []
        user = question if signal is None else \
            f"{question}\n\nCURRENT SIGNAL:\n{json.dumps(signal, ensure_ascii=False)}"
        tools = self._build_gemini_tools()

        async def _call(model: str):
            chat = self.client.aio.chats.create(
                model=model,
                config={
                    "tools": tools,
                    "system_instruction": prompts.COACH_V1,
                    "automatic_function_calling": {"maximum_remote_calls": self.cfg.max_tool_calls},
                }
            )
            return await chat.send_message(user)

        resp, model_used = await self._call_with_fallback(_call, on_retry=self._calls.clear)

        text = resp.text.strip() if resp.text else ""
        usage = _usage_gemini(resp)
        ans = CoachAnswer(text=text, tool_calls=list(self._calls), model=model_used,
                          prompt_version=prompts.COACH_VERSION, usage=usage,
                          stop_reason=None)
        ans.unverified_numbers = parity_check(text, ans.tool_calls)
        if store:
            ans.analysis_id = self._store(
                "suggestion", None, model_used, prompts.COACH_VERSION,
                {"question": question, "signal": signal, "usage": usage,
                 "tool_calls": [{"name": c.name, "args": c.args, "result": c.result} for c in ans.tool_calls],
                 "unverified_numbers": ans.unverified_numbers},
                text)
        return ans

    # ── /review ─────────────────────────────────────────────────────────
    async def review(self, trade_id: int, store: bool = True, with_images: bool = True) -> dict:
        """Structured post-trade review from the pre-assembled review context.

        With ``with_images`` the trade's PRE/POST screenshots (from ``/shot``)
        are attached as image blocks; whatever the model reads off a chart comes
        back in ``chart_observations`` and is stored separately as
        ``ai_analysis.kind='vision'`` — an observation, never a fact."""
        if self.is_gemini:
            return await self._review_gemini(trade_id, store=store, with_images=with_images)
        from pydantic import BaseModel

        class Review(BaseModel):
            facts: list[str]
            metrics: list[str]
            rule_violations: list[str]
            observations: list[str]
            chart_observations: list[str]
            questions: list[str]

        ctx = self.tools.review_context(trade_id)
        if "error" in ctx:
            return ctx
        images, skipped = ([], []) if not with_images else load_screenshots(ctx["trade"].get("screenshots", []))
        content: list[dict] = []
        for img in images:
            content.append({"type": "text", "text": f"Chart screenshot — {img['phase']} ({img['name']}):"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": img["media_type"],
                                                        "data": img["data"]}})
        content.append({"type": "text", "text": "REVIEW CONTEXT (JSON):\n" + json.dumps(ctx, ensure_ascii=False)})
        system = prompts.REVIEW_V1 + (("\n\n" + prompts.REVIEW_VISION_ADDENDUM) if images else
                                      "\n\nNo chart screenshots were provided; leave chart_observations empty.")
        try:
            resp = await self.client.messages.parse(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_format=Review,
            )
        except Exception as e:  # noqa: BLE001
            raise _friendly(e) from e
        parsed = resp.parsed_output
        out = parsed.model_dump() if parsed is not None else {"error": "no structured output"}
        out["trade_id"] = trade_id
        out["model"] = self.cfg.model
        out["prompt_version"] = prompts.REVIEW_VERSION
        out["images_used"] = [i["name"] for i in images]
        out["images_skipped"] = skipped
        out["unverified_numbers"] = parity_check(
            " ".join(sum((out.get(k, []) for k in ("facts", "metrics", "observations")), [])),
            [ToolCall("journal_review_context", {"trade_id": trade_id}, ctx)])
        if store and "error" not in out:
            out["analysis_id"] = self._store("review", trade_id, self.cfg.model, prompts.REVIEW_VERSION,
                                             ctx, json.dumps(out, ensure_ascii=False))
            if images and out.get("chart_observations"):
                out["vision_analysis_id"] = self._store(
                    "vision", trade_id, self.cfg.model, prompts.REVIEW_VERSION,
                    {"images": out["images_used"], "label": "observation"},
                    json.dumps(out["chart_observations"], ensure_ascii=False))
        return out

    async def _review_gemini(self, trade_id: int, store: bool = True, with_images: bool = True) -> dict:
        from pydantic import BaseModel
        from google.genai import types

        class Review(BaseModel):
            facts: list[str]
            metrics: list[str]
            rule_violations: list[str]
            observations: list[str]
            chart_observations: list[str]
            questions: list[str]

        ctx = self.tools.review_context(trade_id)
        if "error" in ctx:
            return ctx
        images, skipped = ([], []) if not with_images else load_screenshots(ctx["trade"].get("screenshots", []))
        contents: list[Any] = []
        for img in images:
            import base64
            contents.append(f"Chart screenshot — {img['phase']} ({img['name']}):")
            raw_data = base64.b64decode(img["data"])
            contents.append(types.Part.from_bytes(data=raw_data, mime_type=img["media_type"]))
        contents.append("REVIEW CONTEXT (JSON):\n" + json.dumps(ctx, ensure_ascii=False))
        system = prompts.REVIEW_V1 + (("\n\n" + prompts.REVIEW_VISION_ADDENDUM) if images else
                                      "\n\nNo chart screenshots were provided; leave chart_observations empty.")
        async def _call(model: str):
            return await self.client.aio.models.generate_content(
                model=model,
                contents=contents,
                config={
                    "system_instruction": system,
                    "response_mime_type": "application/json",
                    "response_schema": Review,
                }
            )

        resp, model_used = await self._call_with_fallback(_call)

        try:
            parsed = Review.model_validate_json(resp.text)
            out = parsed.model_dump()
        except Exception:
            out = {"error": "failed to parse structured review"}

        out["trade_id"] = trade_id
        out["model"] = model_used
        out["prompt_version"] = prompts.REVIEW_VERSION
        out["images_used"] = [i["name"] for i in images]
        out["images_skipped"] = skipped
        out["unverified_numbers"] = parity_check(
            " ".join(sum((out.get(k, []) for k in ("facts", "metrics", "observations")), [])),
            [ToolCall("journal_review_context", {"trade_id": trade_id}, ctx)])
        if store and "error" not in out:
            out["analysis_id"] = self._store("review", trade_id, model_used, prompts.REVIEW_VERSION,
                                             ctx, json.dumps(out, ensure_ascii=False))
            if images and out.get("chart_observations"):
                out["vision_analysis_id"] = self._store(
                    "vision", trade_id, model_used, prompts.REVIEW_VERSION,
                    {"images": out["images_used"], "label": "observation"},
                    json.dumps(out["chart_observations"], ensure_ascii=False))
        return out

    # ── narrative (weekly / monthly report) ─────────────────────────────
    async def narrative(self, system: str, payload: dict, max_tokens: int = 4000) -> tuple[str, list[str], str]:
        """One tool-less call: turn a deterministic metrics block into prose.
        Returns (text, unverified_numbers, model_used)."""
        if self.is_gemini:
            return await self._narrative_gemini(system, payload, max_tokens=max_tokens)
        try:
            resp = await self.client.messages.create(
                model=self.cfg.model,
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": "METRICS (JSON):\n" + json.dumps(payload, ensure_ascii=False)}],
            )
        except Exception as e:  # noqa: BLE001
            raise _friendly(e) from e
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text, parity_check(text, [ToolCall("metrics", {}, payload)]), self.cfg.model

    async def _narrative_gemini(self, system: str, payload: dict, max_tokens: int = 4000) -> tuple[str, list[str], str]:
        async def _call(model: str):
            return await self.client.aio.models.generate_content(
                model=model,
                contents=["METRICS (JSON):\n" + json.dumps(payload, ensure_ascii=False)],
                config={
                    "system_instruction": system,
                    "max_output_tokens": max_tokens,
                }
            )

        resp, model_used = await self._call_with_fallback(_call)
        text = (resp.text or "").strip()
        return text, parity_check(text, [ToolCall("metrics", {}, payload)]), model_used


_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024      # API limit per image
MAX_IMAGES = 4


def load_screenshots(shots: list[dict]) -> tuple[list[dict], list[str]]:
    """Read a trade's screenshot files as base64 image blocks. PRE first, then
    POST; unsupported types, missing or oversized files are reported, not sent."""
    import base64
    from pathlib import Path

    images: list[dict] = []
    skipped: list[str] = []
    order = {"PRE": 0, "POST": 1}
    for s in sorted(shots, key=lambda s: order.get(s.get("phase", ""), 2)):
        p = Path(s["path"])
        mt = _IMAGE_TYPES.get(p.suffix.lower())
        if mt is None:
            skipped.append(f"{p.name}: unsupported type")
            continue
        if not p.exists():
            skipped.append(f"{p.name}: file missing")
            continue
        if p.stat().st_size > MAX_IMAGE_BYTES:
            skipped.append(f"{p.name}: over {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
            continue
        if len(images) >= MAX_IMAGES:
            skipped.append(f"{p.name}: more than {MAX_IMAGES} images")
            continue
        images.append({"phase": s.get("phase", "?"), "name": p.name, "media_type": mt,
                       "data": base64.standard_b64encode(p.read_bytes()).decode("ascii")})
    return images, skipped


def _usage(msg: Any) -> dict:
    u = getattr(msg, "usage", None)
    if u is None:
        return {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: getattr(u, k, None) for k in keys if getattr(u, k, None) is not None}


def _usage_gemini(resp: Any) -> dict:
    meta = getattr(resp, "usage_metadata", None)
    if not meta:
        return {}
    return {
        "input_tokens": getattr(meta, "prompt_token_count", None),
        "output_tokens": getattr(meta, "candidates_token_count", None),
        "total_tokens": getattr(meta, "total_token_count", None),
    }


def format_review(r: dict) -> str:
    """Telegram-friendly text for a ``review()`` result."""
    if "error" in r:
        return f"review failed: {r['error']}"
    parts = [f"Review of trade #{r['trade_id']}"]
    for key, title in (("facts", "FACTS"), ("metrics", "METRICS"), ("rule_violations", "RULE VIOLATIONS"),
                       ("observations", "OBSERVATIONS"), ("chart_observations", "CHART (observations, not facts)"),
                       ("questions", "QUESTIONS")):
        items = r.get(key) or []
        if key == "chart_observations" and not items:
            continue
        parts.append(f"\n{title}\n" + ("\n".join(f"• {x}" for x in items) if items else "• none"))
    if r.get("images_skipped"):
        parts.append(f"\n(screenshots skipped: {'; '.join(r['images_skipped'])})")
    if r.get("unverified_numbers"):
        parts.append(f"\n⚠ numbers not found in the input: {', '.join(r['unverified_numbers'])}")
    return "\n".join(parts)


def chunk(text: str, size: int = 4000) -> list[str]:
    """Split for Telegram's message limit, preferring paragraph breaks."""
    out: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out
