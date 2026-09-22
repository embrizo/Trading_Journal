"""Load and validate config.yaml into typed models."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .core.params import Params


class Watch(BaseModel):
    symbol: str
    timeframe: str


class TelegramCfg(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class DiscordCfg(BaseModel):
    enabled: bool = False
    webhook_url: str = ""


class Channels(BaseModel):
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    discord: DiscordCfg = Field(default_factory=DiscordCfg)


class JournalCfg(BaseModel):
    db: str = "data/journal.db"
    screenshots_dir: str = "data/journal/screenshots"
    symbol_aliases: dict[str, str] = Field(default_factory=lambda: {
        "SOL": "SOL-USDT-SWAP", "BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP",
    })
    history_footer: bool = True
    account_size: float | None = None   # enables risk_pct from risk_amount
    backup_dir: str | None = "data/backups"   # None/"" disables the nightly backup
    backup_time: str = "00:05"                # UTC, HH:MM
    backup_keep: int = 14                     # snapshots retained


class TelegramBotCfg(BaseModel):
    """Command handling (/trade /close /ask …) — separate from alert sending."""
    enabled: bool = False
    allowed_chat_ids: list[str] = Field(default_factory=list)   # only these may write
    poll_timeout: int = 30                                       # getUpdates long-poll seconds


class AiCfg(BaseModel):
    enabled: bool = False
    model: str = "claude-opus-5"
    max_tokens: int = 16000
    max_tool_calls: int = 8          # tool-runner iterations per /ask
    daily_ask_limit: int = 30
    weekly_report: bool = True
    weekly_report_cron: str = "MON 00:15"   # UTC
    api_key: str = ""                # empty → ANTHROPIC_API_KEY from the environment


class WebCfg(BaseModel):
    """The journal's HTTP server: dashboard + webhook share one port."""
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8787
    dashboard: bool = True    # dashboard at / (LAN only — reads need no auth)
    write_token: str = ""     # set to enable /api/do (log/close/edit from the page).
                              # Empty = the dashboard stays read-only. Sent as
                              # X-Journal-Token; reads are unaffected either way.


class WebhookCfg(BaseModel):
    """TradingView alert webhook → journal signals (source='pine'), mounted at /pine/<secret>."""
    enabled: bool = False
    secret: str = ""          # required when enabled; goes in the URL path
    notify: bool = False      # also push each new pine alert through the Telegram/Discord notifiers


class Config(BaseModel):
    exchange: str = "okx"
    watches: list[Watch]
    params: dict = Field(default_factory=dict)
    backfill: int = 500
    channels: Channels = Field(default_factory=Channels)
    render_chart: bool = True
    chart_bars: int = 120
    state_db: str = "state.db"
    log_level: str = "INFO"
    journal: JournalCfg = Field(default_factory=JournalCfg)
    telegram_bot: TelegramBotCfg = Field(default_factory=TelegramBotCfg)
    ai: AiCfg = Field(default_factory=AiCfg)
    web: WebCfg = Field(default_factory=WebCfg)
    webhook: WebhookCfg = Field(default_factory=WebhookCfg)

    def to_params(self) -> Params:
        # Only pass keys Params knows about, so extra yaml keys don't crash startup.
        known = {f for f in Params.__dataclass_fields__}  # type: ignore[attr-defined]
        return Params(**{k: v for k, v in self.params.items() if k in known})


def load_config(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Config(**data)


# OKX bar string -> seconds, for pivot auto-tuning.
_BAR_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "2H": 7200, "4H": 14400, "6H": 21600, "12H": 43200,
    "1D": 86400, "2D": 172800, "3D": 259200, "1W": 604800,
}


def bar_seconds(bar: str) -> int:
    if bar not in _BAR_SECONDS:
        raise ValueError(f"Unknown OKX bar '{bar}'")
    return _BAR_SECONDS[bar]
