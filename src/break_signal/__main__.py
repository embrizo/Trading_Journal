"""Entry point: start one watcher per configured (symbol, timeframe)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import load_config
from .core.state import State
from .journal.db import JournalDB
from .notify.base import Notifier
from .notify.discord import DiscordNotifier
from .notify.telegram import TelegramNotifier
from .watcher import Watcher


def build_notifiers(cfg) -> list[Notifier]:
    out: list[Notifier] = []
    tg = cfg.channels.telegram
    if tg.enabled and tg.bot_token and tg.chat_id:
        out.append(TelegramNotifier(tg.bot_token, tg.chat_id))
    dc = cfg.channels.discord
    if dc.enabled and dc.webhook_url:
        out.append(DiscordNotifier(dc.webhook_url))
    return out


async def run(config_path: str) -> None:
    cfg = load_config(config_path)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("break_signal")

    notifiers = build_notifiers(cfg)
    if not notifiers:
        log.warning("No notifiers configured — breaks will be logged only.")
    else:
        log.info("Notifiers: %s", ", ".join(n.name for n in notifiers))

    state = State(cfg.state_db)
    journal = JournalDB(cfg.journal.db, account_size=cfg.journal.account_size)
    log.info("Journal: %s", cfg.journal.db)
    watchers = [Watcher(cfg, w, state, notifiers, journal) for w in cfg.watches]
    tasks = [w.run() for w in watchers]
    from .journal.tools import Tools
    tools = Tools(journal, cfg)
    coach = build_coach(cfg, tools, log)
    bot = build_telegram_bot(cfg, tools, coach, log)
    if bot is not None:
        tasks.append(bot.run())
    if cfg.ai.weekly_report and notifiers:
        from .journal.report import run_scheduler
        tasks.append(run_scheduler(cfg, journal, notifiers, coach))
    if cfg.journal.backup_dir:
        from .journal.backup import run_scheduler as run_backups
        tasks.append(run_backups(cfg, journal))
    if cfg.web.enabled or cfg.webhook.enabled:
        from .journal.web import serve as serve_web
        tasks.append(serve_web(cfg, journal, notifiers, tools))
    log.info("Starting %d watcher(s)%s%s%s%s", len(watchers), " + telegram bot" if bot else "",
             " + report scheduler" if cfg.ai.weekly_report and notifiers else "",
             " + nightly backup" if cfg.journal.backup_dir else "",
             f" + web :{cfg.web.port}" if cfg.web.enabled or cfg.webhook.enabled else "")
    try:
        await asyncio.gather(*tasks)
    finally:
        state.close()
        journal.close()


def build_coach(cfg, tools, log):
    """The AI coach when ai.enabled, a key is available and the SDK is installed; else None."""
    if not cfg.ai.enabled:
        return None
    key = cfg.ai.api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log.warning("ai.enabled but no API key (ai.api_key or GEMINI_API_KEY / ANTHROPIC_API_KEY) — /ask and report notes disabled")
        return None
    try:
        from .journal.coach import Coach
        coach = Coach(tools, cfg.ai)
    except ImportError as e:
        log.warning("ai.enabled but AI SDK missing: %s", e)
        return None
    log.info("AI coach: %s", cfg.ai.model)
    return coach


def build_telegram_bot(cfg, tools, coach, log):
    """Command bot, or None when disabled / no token."""
    tb = cfg.telegram_bot
    if not tb.enabled:
        return None
    token = cfg.channels.telegram.bot_token
    if not token:
        log.warning("telegram_bot.enabled but channels.telegram.bot_token is empty — bot not started")
        return None
    if not tb.allowed_chat_ids:
        log.warning("telegram_bot.allowed_chat_ids is empty — nobody can use the bot")
    from .notify.telegram_bot import TelegramBot
    return TelegramBot(token, cfg, tools, coach)


def main() -> None:
    ap = argparse.ArgumentParser(prog="break_signal", description="OKX trendline breakout alerts")
    ap.add_argument(
        "-c", "--config",
        default=os.environ.get("BREAK_SIGNAL_CONFIG", "config.yaml"),
        help="path to config.yaml (default: ./config.yaml or $BREAK_SIGNAL_CONFIG)",
    )
    args = ap.parse_args()
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
