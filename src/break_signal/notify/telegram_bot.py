"""Telegram command bot: log trades from the phone, ask the coach.

Long-polls ``getUpdates`` with the same bot token used for alerts. Only
``telegram_bot.allowed_chat_ids`` may talk to it. All writes go through
``journal.tools.Tools`` (rule checks, auto-link, seeded rules identical to the
CLI and MCP paths); ``/ask`` and ``/review`` go to the read-only coach.

``handle_command()`` is pure (text in → reply out) so it is unit-testable
without Telegram; only ``run()`` / ``_api()`` touch the network.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from ..journal import analytics
from ..journal.tools import Tools

if TYPE_CHECKING:
    from ..config import Config
    from ..journal.coach import Coach

log = logging.getLogger(__name__)

HELP = """Journal commands
/trade <SYM> [tf] long|short <entry> [sl x] [tp x] [size x] [risk x%] [#tags] [-- reason]
/close <id> <exit> [win|loss|be] [#tags] [reason]
/skip <signal_id> [reason]
/event <id> sl_moved|tp_moved|partial_close|added|note [k=v ...]
/tag <id> entry|exit #tag ...
/shot <id> pre|post   (as the caption of a photo)
/list [30d|6m|all]   /show <id>   /stats [period]   /signals   /tags   /rules
/report [weekly|monthly]   /memories   /confirm <id>   /forget <id>
/ask <question>      /review <id>     (AI coach, read-only)
Example: /trade SOL 4H long 231.5 sl 225 tp 245 #breakout #retest -- clean retest"""

_COACH_OFF = "AI coach is off (ai.enabled: false, or no key: GEMINI_API_KEY / ANTHROPIC_API_KEY / ai.api_key)"


def _f(x: float | None, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, str):
        return x
    return f"{x:.{nd}f}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _violations(vs: list[dict]) -> str:
    return "".join(f"\n⚠ {v['name']}: {v['detail']}" for v in vs)


def _trade_line(t: dict) -> str:
    tags = " ".join(f"#{x}" for x in t["entry_tags"] + t["exit_tags"])
    r = f" R={_f(t['r_multiple'])}" if t["r_multiple"] is not None else ""
    return (f"#{t['id']} {t['status']} {t['symbol']} {t['tf'] or ''} {t['direction']} "
            f"@{_f(t['entry_price'], 4)} sl {_f(t['sl_price'], 4)} tp {_f(t['tp_price'], 4)}"
            f"{' → ' + t['outcome'] if t['outcome'] else ''}{r} {tags}").strip()


class TelegramBot:
    def __init__(self, bot_token: str, cfg: "Config", tools: Tools, coach: "Coach | None" = None):
        self._token = bot_token
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self.cfg = cfg
        self.tools = tools
        self.coach = coach
        self.allowed = {str(c) for c in cfg.telegram_bot.allowed_chat_ids}
        self.screens = Path(cfg.journal.screenshots_dir)
        self._offset = 0

    # ── network ─────────────────────────────────────────────────────────
    async def _api(self, session: aiohttp.ClientSession, method: str, **payload: Any) -> Any:
        timeout = aiohttp.ClientTimeout(total=self.cfg.telegram_bot.poll_timeout + 15)
        async with session.post(f"{self._base}/{method}", json=payload, timeout=timeout) as r:
            data = await r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    async def send(self, session: aiohttp.ClientSession, chat_id: str, text: str) -> None:
        from ..journal.coach import chunk
        for part in chunk(text):
            await self._api(session, "sendMessage", chat_id=chat_id, text=part)

    async def run(self) -> None:
        log.info("telegram bot: polling (allowed chats: %s)", ", ".join(sorted(self.allowed)) or "none")
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    updates = await self._api(session, "getUpdates", offset=self._offset,
                                              timeout=self.cfg.telegram_bot.poll_timeout,
                                              allowed_updates=["message"])
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                    log.warning("telegram bot: getUpdates failed: %s", e)
                    await asyncio.sleep(5)
                    continue
                for upd in updates:
                    self._offset = max(self._offset, upd["update_id"] + 1)
                    try:
                        await self._handle_update(session, upd)
                    except Exception:  # noqa: BLE001 — one bad message never kills the bot
                        log.exception("telegram bot: update failed")

    async def _handle_update(self, session: aiohttp.ClientSession, upd: dict) -> None:
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not chat_id:
            return
        if chat_id not in self.allowed:
            log.warning("telegram bot: ignoring chat %s (not in allowed_chat_ids)", chat_id)
            return
        photo: bytes | None = None
        text = msg.get("text") or msg.get("caption") or ""
        if msg.get("photo"):
            photo = await self._download_photo(session, msg["photo"])
        reply = await self.handle_command(chat_id, text, photo)
        if reply:
            await self.send(session, chat_id, reply)

    async def _download_photo(self, session: aiohttp.ClientSession, sizes: list[dict]) -> bytes:
        file_id = max(sizes, key=lambda p: p.get("file_size", 0))["file_id"]
        info = await self._api(session, "getFile", file_id=file_id)
        url = f"https://api.telegram.org/file/bot{self._token}/{info['file_path']}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
            r.raise_for_status()
            return await r.read()

    # ── commands (pure: text in, reply out) ─────────────────────────────
    async def handle_command(self, chat_id: str, text: str, photo: bytes | None = None) -> str:
        text = (text or "").strip()
        if not text and photo is None:
            return ""
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        rest = rest.strip()
        try:
            if photo is not None:
                return self._cmd_shot(rest if cmd == "/shot" else text, photo)
            handler = getattr(self, "_cmd_" + cmd.lstrip("/"), None) if cmd.startswith("/") else None
            if handler is None:
                return HELP if cmd in ("/start", "/help") or not cmd.startswith("/") else f"unknown command {cmd}\n\n{HELP}"
            result = handler(rest)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except (ValueError, KeyError) as e:
            return f"error: {e.args[0] if isinstance(e, KeyError) and e.args else e}"

    def _cmd_start(self, rest: str) -> str:
        return HELP

    _cmd_help = _cmd_start

    def _cmd_trade(self, rest: str) -> str:
        if not rest:
            return "usage: /trade <SYM> [tf] long|short <entry> [sl x] [tp x] [#tags] [-- reason]"
        out = self.tools.add_trade_line(rest)
        t = out["trade"]
        link = f"\nlinked to signal #{out['auto_linked_signal']}" if out["auto_linked_signal"] else ""
        return (f"logged {_trade_line(t)}\nplanned R:R {_f(t['planned_rr'])}{link}"
                f"{_violations(out['rule_violations'])}")

    def _cmd_close(self, rest: str) -> str:
        if not rest:
            return "usage: /close <id> <exit> [win|loss|be] [#tags] [reason]"
        out = self.tools.close_trade_line(rest)
        t = out["trade"]
        note = f"\n⚠ {out['outcome_note']}" if out.get("outcome_note") else ""
        return (f"closed #{t['id']}: {t['outcome']} R={_f(t['r_multiple'])} pnl={_f(t['pnl_amount'])}"
                f"{note}{_violations(out['rule_violations'])}")

    def _cmd_skip(self, rest: str) -> str:
        sid, _, reason = rest.partition(" ")
        if not sid.isdigit():
            return "usage: /skip <signal_id> [reason]"
        t = self.tools.skip_signal(int(sid), reason.strip() or None)["trade"]
        return f"recorded skip #{t['id']} on signal #{sid} ({t['symbol']} {t['tf']} {t['direction']})"

    def _cmd_event(self, rest: str) -> str:
        parts = rest.split()
        if len(parts) < 2 or not parts[0].isdigit():
            return "usage: /event <trade_id> <type> [key=value ...]"
        data: dict[str, Any] = {}
        for kv in parts[2:]:
            k, _, v = kv.partition("=")
            try:
                data[k] = float(v)
            except ValueError:
                data[k] = v
        out = self.tools.add_event(int(parts[0]), parts[1], data)
        return f"event on #{parts[0]}: {parts[1]} {data}{_violations(out['rule_violations'])}"

    def _cmd_tag(self, rest: str) -> str:
        parts = rest.split()
        if len(parts) < 3 or not parts[0].isdigit() or parts[1].upper() not in ("ENTRY", "EXIT"):
            return "usage: /tag <trade_id> entry|exit #tag ..."
        tags = [p.lstrip("#").replace("_", " ") for p in parts[2:]]
        t = self.tools.tag_trade(int(parts[0]), tags, parts[1].upper())["trade"]
        return f"#{t['id']} entry tags {t['entry_tags']} exit tags {t['exit_tags']}"

    def _cmd_tags(self, rest: str) -> str:
        by: dict[str, list[str]] = {}
        for t in self.tools.list_tags():
            by.setdefault(t["category"], []).append(t["name"])
        return "\n".join(f"{c}: {', '.join(n)}" for c, n in by.items())

    def _cmd_rules(self, rest: str) -> str:
        rows = self.tools.list_rules()
        return "\n".join(f"{'✓' if r['enabled'] else '✗'} #{r['id']} {r['name']} ({r['severity']})" for r in rows) or "no rules"

    def _cmd_list(self, rest: str) -> str:
        period = rest or None
        analytics.period_to_since(period)  # validates
        out = self.tools.search_trades(period=period, limit=10)
        if not out["trades"]:
            return "no trades"
        return "\n".join(_trade_line(t) for t in out["trades"])

    def _cmd_show(self, rest: str) -> str:
        if not rest.isdigit():
            return "usage: /show <id>"
        t = self.tools.get_trade(int(rest))
        if "error" in t:
            return t["error"]
        lines = [_trade_line(t),
                 f"opened {t['opened'] or '-'}  closed {t['closed'] or '-'}  session {t['ctx_session'] or '-'}",
                 f"planned R:R {_f(t['planned_rr'])}  risk {_f(t['risk_amount'])} ({_f(t['risk_pct'])}%)  conf {t['confidence'] or '-'}"]
        if t["entry_reason"]:
            lines.append(f"entry: {t['entry_reason']}")
        if t["exit_reason"]:
            lines.append(f"exit: {t['exit_reason']}")
        if t["signal"]:
            s = t["signal"]
            lines.append(f"signal #{s['id']}: {s['event']} {s['side']} rsi {s['rsi']} vol {s['vol_ratio']}")
        for e in t["events"]:
            lines.append(f"event {e['ts']}: {e['type']} {e['data']}")
        for v in t["rule_violations"]:
            lines.append(f"⚠ {v['name']}: {v['detail']}")
        return "\n".join(lines)

    def _cmd_stats(self, rest: str) -> str:
        period = rest or "all"
        s = self.tools.stats(period)
        st = s["streaks"]
        lines = [f"Stats ({period}): n={s['n']} W/L/BE {s['wins']}/{s['losses']}/{s['be']} win {_pct(s['win_rate'])}",
                 f"avg R {_f(s['avg_r'])}  total R {_f(s['total_r'])}  PF {_f(s['profit_factor'])}  max DD {_f(s['max_drawdown_r'])}R",
                 f"streak: {st['current']} {st['current_kind'] or ''} (max win {st['max_win']}, max loss {st['max_loss']})"]
        tags = self.tools.tag_stats(period, "ENTRY")["tags"]
        if tags:
            lines.append("by entry tag:")
            for name, v in list(tags.items())[:8]:
                lines.append(f"  {name}: n={v['n']} win {_pct(v['win_rate'])} avg R {_f(v['avg_r'])}")
        return "\n".join(lines)

    def _cmd_signals(self, rest: str) -> str:
        limit = int(rest) if rest.isdigit() else 8
        sigs = self.tools.recent_signals(limit=limit)
        if not sigs:
            return "no signals"
        return "\n".join(f"#{s['id']} {s['source']} {s['symbol']} {s['tf']} {s['event']} {s['side']} "
                         f"@{_f(s['price'], 4)} rsi {_f(s['rsi'], 1)} {s['candle']}" for s in sigs)

    def _cmd_shot(self, rest: str, photo: bytes) -> str:
        parts = rest.replace("/shot", "").split()
        if len(parts) < 2 or not parts[0].isdigit() or parts[1].lower() not in ("pre", "post"):
            return "caption must be: /shot <trade_id> pre|post"
        trade_id, phase = int(parts[0]), parts[1].upper()
        self.screens.mkdir(parents=True, exist_ok=True)
        path = self.screens / f"{trade_id}_{phase}_{int(time.time() * 1000)}.jpg"
        path.write_bytes(photo)
        self.tools.add_screenshot(trade_id, phase, str(path))
        return f"saved {phase} screenshot for #{trade_id}: {path.name}"

    def _cmd_memories(self, rest: str) -> str:
        from ..journal.memory import format_memories
        return format_memories(self.tools.memories(refresh=True))

    def _cmd_confirm(self, rest: str) -> str:
        if not rest.isdigit():
            return "usage: /confirm <memory_id>"
        m = self.tools.confirm_memory(int(rest))
        return f"confirmed #{m['id']}: {m['content']}"

    def _cmd_forget(self, rest: str) -> str:
        if not rest.isdigit():
            return "usage: /forget <memory_id>"
        self.tools.forget_memory(int(rest))
        return f"forgot #{rest}"

    async def _cmd_report(self, rest: str) -> str:
        from ..journal import report as R
        kind = rest.strip().lower() or "weekly"
        if kind not in R.PERIOD_DAYS:
            return "usage: /report [weekly|monthly]"
        from ..journal.coach import CoachError
        try:
            out = await R.generate(self.tools.db, kind, self.coach, store=True)
        except CoachError as e:
            out = await R.generate(self.tools.db, kind, None, store=True)
            out["markdown"] += f"\n\n(coach notes unavailable: {e})"
        return out["markdown"]

    async def _cmd_ask(self, rest: str) -> str:
        if self.coach is None:
            return _COACH_OFF
        if not rest:
            return "usage: /ask <question>"
        from ..journal.coach import AskBudgetExceeded, CoachError
        try:
            ans = await self.coach.ask(rest)
        except (AskBudgetExceeded, CoachError) as e:
            return f"coach: {e}"
        note = f"\n\n⚠ numbers not found in tool results: {', '.join(ans.unverified_numbers)}" \
            if ans.unverified_numbers else ""
        calls = f"\n\n[{len(ans.tool_calls)} tool calls · {ans.model} · {ans.prompt_version}]"
        return ans.text + note + calls

    async def _cmd_review(self, rest: str) -> str:
        if self.coach is None:
            return _COACH_OFF
        if not rest.isdigit():
            return "usage: /review <trade_id>"
        from ..journal.coach import CoachError, format_review
        try:
            return format_review(await self.coach.review(int(rest)))
        except CoachError as e:
            return f"coach: {e}"
