# ANTIGRAVITY_HANDOFF.md — Trading Journal

**Last Updated:** 2026-09-23 (session 7)
**Workspace:** `D:\etc\Program\7Days\Trading_Journal` (fresh clone this session)
**Repo:** https://github.com/embrizo/Trading_Journal (`main`) — old `Break_Signal` remote is archived
**Tech Stack:** Python 3.11+ (asyncio, numpy, pandas, aiohttp, websockets, pydantic, mcp) · Pine Script v6 · SQLite (WAL) · Docker (ARM64 / Raspberry Pi 5) · TradingView Lightweight Charts

---

## Status: Journal + AI Coach Built (J0–J6), 4 Real Positions Logged (2026-09-23)

Everything in [`JOURNAL_AI_IMPLEMENTATION_PLAN.md`](JOURNAL_AI_IMPLEMENTATION_PLAN.md) §5 (J0–J6) is implemented and unit-tested (**236 unit tests pass, 3 live Anthropic-evals skipped**).
The three front-ends (CLI, Claude Code MCP server, Telegram bot) share one `Tools` surface in `src/break_signal/journal/tools.py`.
`analytics.py` is the single source of truth where metrics and numbers are computed.

### Recent Session Updates (2026-09-23)
- **First 4 real open positions logged** in `data/journal.db` (gitignored). Screenshots placed in `pic/` (gitignored).
- **Three key fixes applied & tested:**
  1. `analytics.r_multiple`: Prevents negative risk when stops are trailed past entry (`risk <= 0 -> None`) to stop winning trades from inverting into huge negative R (LOSS). `sl_price` stores initial stop; trailing moves are stored as `sl_moved` events.
  2. `db.update_trade`: Automatically computes `risk_pct` from `risk_amount` when `account_size` is configured.
  3. `tools.update_trade`: Re-runs `rules.check` and `record_violations` on trade updates so rule breaches/clears are always persisted.
- **`.gitignore` fixed**: inline comment on `pic/` separated so git properly ignores exchange screenshots.
- **Gemini API integration shipped** (commit `24aa7a5`, same day) — `journal/coach.py` now
  supports `ai.provider: auto | gemini | anthropic`. This was previously tracked below as a
  future step; it's done.

### Session 7 (2026-09-23, fresh clone / new machine)
- Ran the full service locally end to end: installed missing deps (`pandas`, `mplfinance`,
  `matplotlib`), created a local `config.yaml` from the example, set `exchange: binance`
  (OKX returned HTTP 403 from this machine's ISP too). Dashboard, watchers, and live WS
  streaming all confirmed working at `http://127.0.0.1:8787/`.
- **Telegram command bot verified live** with a real bot token — `channels.telegram` and
  `telegram_bot` enabled, `allowed_chat_ids` restricted to the user's chat id (found via
  `getUpdates`), `/help` and `/stats` round-tripped for real. This was previously an open
  live-check item; it's now done. Discord webhook and a live LLM call are still unverified.

---

## 1. Project Overview & Architecture

### A. Break Signal Engine (Trendline & Breakout Detection)
- Automated support/resistance detection on `OKX:SOLUSDT.P` (1D and 4H timeframes).
- Multi-scale fractal pivots (coarse + fine `pivot_len_fine=3`).
- Validity filter walks to `last_bar - 1` (Pine parity).
- Confirmed close breakout filter (ATR buffer, volume, body ratio).
- Read-only public market data (no exchange keys, never places orders).

### B. Trading Journal & AI Coach
- **`src/break_signal/journal/`**:
  - `db.py`: SQLite WAL mode with migration tracking (`_MIGRATIONS`, schema v3).
  - `models.py`: Dataclasses for trades, events, tags, rules, memories, and signals.
  - `parser.py`: One-line trade syntax (`SOL 4H long 231.5 sl 225 tp 245 #breakout`).
  - `analytics.py`: ONLY place metrics are computed.
  - `rules.py`: Direction-aware rule evaluation engine (`sl_widened`, `max risk %`, etc.).
  - `similar.py`: Deterministic nearest-neighbor trade ranking.
  - `tools.py`: Shared unified `Tools` interface for CLI, MCP, and Telegram.
  - `mcp_server.py`: FastMCP stdio server exposing 29 tools for AI assistants.
  - `web.py` + `static/dashboard.html`: aiohttp dashboard with Lightweight Charts + webhook.
  - `webhook.py`: TradingView alert receiver endpoint (`/pine/<secret>`).
  - `backup.py` & `export.py`: Automated database snapshots and markdown/CSV/JSON exports.
  - `coach.py`: AI coach integration (Anthropic currently; Gemini migration planned).

---

## 2. Key Architecture Invariants & Rules

1. **`journal/analytics.py` is the only source of truth for numbers**: Never compute R, PnL, win-rates, or drawdown anywhere else.
2. **`sl_price` is the initial stop**: Never overwrite `sl_price` with a trailing stop. Record trailing moves as `sl_moved` events.
3. **All front-end writes go through `journal/tools.py`**: Ensures rules, migrations, auto-linking, and validation are enforced.
4. **Pine ↔ Python parity**: `trendline._build_side` stops at `last_bar - 1`. Parity in pivots (`use_fine_pivots`, `pivot_len_fine=3`).
5. **No secret or private data in git**: `config.yaml`, `.env`, `data/journal.db`, and `pic/` must remain gitignored.
6. **Gemini backend migration**: The coach's LLM interface in `journal/coach.py` is slated to support/switch to Google Gemini API.

---

## 3. Quick Run & Verification Commands

```bash
# Run all unit tests
python -m pytest tests/ -q

# Journal CLI operations
python -m break_signal.journal stats
python -m break_signal.journal memories list
python -m break_signal.journal backup
python -m break_signal.journal report --dry-run

# Start full service (watchers + dashboard + webhook)
python -m break_signal -c config.yaml   # dashboard at http://127.0.0.1:8787/

# Docker build (Pi 5)
docker compose up -d --build
```

---

## 4. Prioritized Next Steps

1. **CLI `update` command**:
   - Add CLI support for `update_trade` (currently available only on `Tools` / MCP).
2. **Live Verification** (Telegram bot done 2026-09-23; the rest still open):
   - A real LLM call through the coach (`/ask`, `/review`, report narrative) with either
     `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` set — untested live on any machine so far.
   - Discord webhook — same pattern as Telegram, not yet wired to a real webhook URL.
   - TradingView Pine indicator verification and parameter tuning (`pivotLen`, `atrBreak`).
   - Raspberry Pi 5 Docker deployment.
   - OKX from a machine where it isn't blocked (403/DNS-blocked on two machines so far).
3. **M6 Multi-Symbol Backtest Report**:
   - Extend `replay.py` across SOL, BTC, ETH to verify default parameters.
