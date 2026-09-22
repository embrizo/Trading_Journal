# Trading Journal

A trade journal with an alert engine attached, for **OKX perpetual futures**.

**Break Signal**, the engine, finds valid support/resistance trendlines with no
manual drawing, watches every candle *close*, and pushes a high-conviction
breakout alert (with a chart) to **Telegram** and **Discord**. Every alert is
stored, so the [journal](#trade-journal) can link what you actually did to the
setup that prompted it, and the [AI coach](#ai-coach-in-claude-code) can answer
"what does my history say?" from numbers it never computes itself.

Two implementations of the same alert algorithm:

- **Phase 1 — TradingView Pine indicator** (`pine/break_signal.pine`): draws the
  lines and fires alerts inside TradingView. Best for eyeballing/tuning the rules.
- **Phase 2 — Python watcher service** (`src/break_signal/`): runs 24/7 (built for
  a Raspberry Pi 5), scans multiple symbols/timeframes with no alert cap, renders a
  chart image, and fans out to Telegram + Discord.

See [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the full algorithm spec,
architecture, and milestones.

## How it works (strict / high-conviction mode)

1. **Fractal pivots** (lookback auto-tuned: 5 on 1D, 8 on 4H).
2. **Pairwise candidate lines** between pivots ≥10 bars apart.
3. **Validity filter** — any candle that *closed* through a line kills it (0 tolerated).
4. **Score** `3×touches + 2×span + 1.5×recency`, require ≥3 touches, keep top 3 per side, dedupe.
5. **Break** on a confirmed close clearing the line by 0.30×ATR, with volume > 1.5× SMA20 and body ≥ 50% of range.

The Python core (`src/break_signal/core/`) is a faithful port of the Pine script,
so both produce the same signals on the same candles.

## Quick start (local)

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # then fill in Telegram/Discord secrets
python -m break_signal -c config.yaml
```

`config.yaml` is gitignored — your bot token and webhook URL never get committed.
No exchange API keys are needed; the service only reads public OKX market data and
never places orders.

## Run on a Raspberry Pi 5 (Docker)

```bash
cp config.example.yaml config.yaml   # fill in secrets
docker compose up -d --build
docker compose logs -f
```

The image is `python:3.11-slim-bookworm` (multi-arch) and pulls prebuilt ARM
wheels from piwheels, so the build is minutes, not an hour. Everything that must
survive a rebuild lives in `./data` (back it with an SSD/USB, not the SD card):

```
data/state.db               alert dedupe state
data/journal.db             the trade journal
data/journal/screenshots/   photos sent to the Telegram bot
data/backups/               nightly journal-<stamp>.db + .md (journal.backup_time, 14 kept)
```

For the AI coach in the container, build with the SDK and pass the key through:

```bash
AI_ENABLED=1 ANTHROPIC_API_KEY=sk-ant-... docker compose up -d --build
```

(or put both in a `.env` file next to `docker-compose.yml`). `OKX_REST_URL` /
`OKX_WS_URL` in the same place switch to a regional OKX host if the default is
geo-blocked where the Pi lives.

The nightly backup is an online SQLite snapshot (`journal backup` does the same
by hand; `journal backup --verify <file>` integrity-checks one). Every snapshot
comes with a markdown twin, so the history is readable without any tooling.

## Backtest the rules

```bash
python -m break_signal.backtest.replay --symbol SOL-USDT-SWAP --tf 1D --limit 500 --out signals.csv
```

Walks a growing window so each bar sees only past data — no look-ahead, same
pivot-confirmation lag as live. Add `--to-journal` to also store the signals in
`data/journal.db` (source `backtest`) so the journal has history before your first
trade; `python -m break_signal.journal import-signals signals.csv` does the same
for an existing CSV.

## Trade journal

Log what you did with each alert and *why*, so the numbers can talk back. Stored in
`data/journal.db` (separate from alert state). See
[`JOURNAL_AI_IMPLEMENTATION_PLAN.md`](JOURNAL_AI_IMPLEMENTATION_PLAN.md) for the
roadmap (AI coach in Claude Code, Telegram commands, alert footers).

```bash
# one-line syntax: <SYM> [<tf>] long|short <entry> [sl x] [tp x] [size x] [risk x[%]] [#tags] [-- reason]
python -m break_signal.journal add "SOL 4H long 231.5 sl 225 tp 245 #breakout #retest -- clean retest"
python -m break_signal.journal close "1 244 win hit TP, held the plan #hit_tp"
python -m break_signal.journal event 1 sl_moved from=225 to=222
python -m break_signal.journal list --period 30d
python -m break_signal.journal show 1
python -m break_signal.journal stats --by tags        # win rate, PF, avg R, drawdown, per-tag
python -m break_signal.journal export --format md --out journal.md
python -m break_signal.journal tag list
```

Quote the whole line (PowerShell eats bare `--` and splits on commas otherwise).
Give `sl_moved` both `from=` and `to=`: the *"Never widen the stop"* rule compares them
against your direction, so trailing a stop toward entry is not flagged — only moving it
away is. Without the numbers the rule is reported as not-applicable rather than broken.

**`sl_price` is the stop you opened with, not your current one.** R is measured against
initial risk, so record every stop change as an `sl_moved` event and leave `sl_price`
alone. A stop trailed past entry has negative risk: the journal refuses to produce an R
for it rather than reporting an inverted one.
R-multiple, PnL and every statistic are computed by `journal/analytics.py`; you
never type them. Anything you don't say is stored as NULL, not guessed. Tags are
free-form (Thai works) and unknown ones are created on the fly.

Every alert the watcher sends is also stored as a signal, and trades link to it
(`add ... --signal 12`, or automatically when logged within 3 bars of a matching
alert). Once you have ≥3 closed trades on a setup, alerts gain a footer:

```
📒 Your history on 4H resistance breaks (LONG): 7 trades · 57% win · +0.8R avg
   Best tag: Retest (+1.4R, n=4)  Worst tag: FOMO (-1.0R, n=3)
   Log: journal add "SOL-USDT-SWAP 4H long <entry> sl <sl> tp <tp>" --signal 118, or skip 118 <reason>
```

`journal footer <signal_id>` previews it; `journal.history_footer: false` turns it off.

### AI coach in Claude Code

`.mcp.json` registers a `journal` MCP server (`pip install -e .[ai]` for the `mcp`
package). Open Claude Code in this repo and talk to it:

> "took the SOL 4H break, long 231.5, sl 225, tp 245, breakout + retest, felt calm"
> "closed 12 at 244, hit TP, held the plan"
> "SOL just broke the 1D resistance at 236, RSI 68 — what does my history say?"

Claude reads `CLAUDE.md` for the coach rules: every number comes from a tool
result with its sample size, past trades are cited by id, suggestions are
conditional price-action to watch — never "buy" or "sell" — and it asks before
writing to the journal. `market_snapshot` needs OKX reachable (`OKX_REST_URL`
env to switch host if geo-blocked).

### Telegram bot + AI coach on the phone

Same bot token as the alerts. In `config.yaml` set `telegram_bot.enabled: true` and
put your chat id in `allowed_chat_ids` (nobody else can talk to it), then run the
service as usual — the bot polls alongside the watchers.

```
/trade SOL 4H long 231.5 sl 225 tp 245 #breakout #retest -- clean retest
/close 12 244 hit TP, held the plan #hit_tp
/skip 118 not at desk          /event 12 sl_moved from=225 to=222
/list 30d   /show 12   /stats   /signals   /tags   /rules
/ask SOL just broke the 1D resistance, RSI 68 — what does my history say?
/review 12
```

`/report [weekly|monthly]` gives the review (metrics this period vs all time,
best/worst trade, tags, rule violations); with the coach on, a short narrative is
appended. The same report is pushed automatically at `ai.weekly_report_cron`
(default `MON 00:15` UTC) and on the 1st of each month. `/memories` lists what the
journal has learned — evidence-backed only: a tag or setup with 5+ trades and a
lopsided result, or a rule broken 3+ times — each with the trade ids behind it.
`/confirm <id>` keeps one for good; `/forget <id>` drops it.

`/ask` and `/review` need `ai.enabled: true` and `ANTHROPIC_API_KEY` (or
`ai.api_key`) plus `pip install -e .[ai]`. `/review` also attaches the trade's
`/shot` screenshots; whatever the model reads off a chart comes back under
*CHART (observations, not facts)* and is stored separately.

### Dashboard and TradingView webhook

Set `web.enabled: true` and open `http://<pi>:8787/` on your LAN: live candles
with the engine's trendlines drawn on them, ▲/▼ markers for stored alerts, ◆/■
markers for your entries and exits, equity curve, stats, tags, memories, recent
alerts. Reading needs no password — don't expose it to the internet.

Set `web.write_token` to a long random string and the page also gets a command
bar, so you can journal from the browser with the same one-line syntax as the CLI
and the bot:

```
add SOL 4H long 231.5 sl 225 tp 245 #breakout #retest -- clean retest
close 5 245 hit TP, held the plan #hit_tp
sl 5 228                 # logs an sl_moved event; your initial stop is kept, so R stays honest
event 5 partial_close size=0.5 price=240
tag 5 exit #hit_tp       note 5 felt calm
skip 11 not at desk
```

Each open trade also gets close / sl / tag / note buttons that *fill in* the box
rather than firing, so a mis-click writes nothing. Commands go through the same
`Tools` layer as everywhere else, so rule checks, auto-linking and alert context
behave identically, and any rule you break comes back in the reply. Without the
token every write is refused with 403, and with no token configured the route
does not exist at all.

The same server can receive the Pine indicator's alerts: set `webhook.enabled`
and a long `webhook.secret`, point a TradingView alert's webhook URL at
`http://<host>:8787/pine/<secret>`, and every break the indicator fires lands in
the journal as a `pine` signal (with `webhook.notify` it is also pushed to
Telegram/Discord with the history footer). Only that path needs a route in from
the internet. The reply says what happened — a stored signal is `200`, a body that
isn't the indicator's `alert()` JSON (the default TradingView message, say) is a
`400` naming the problem, so a misconfigured alert shows up in TradingView's log
instead of silently storing nothing. The coach only has **read-only** tools —
it can never log or edit a trade — and every answer is stored in `ai_analysis`
with the model, prompt version and the exact tool results it saw. Numbers in a
reply that don't appear in any tool result are flagged with ⚠. Try it without
Telegram: `python -m break_signal.journal ask "how are my 4H breaks?"`.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

The core tests (`tests/test_indicators.py`, `test_pivots.py`, `test_trendline.py`,
`test_breakout.py`) need only numpy + pytest and verify the algorithm on synthetic
data with a known trendline. The journal tests (`test_journal_db.py`, `test_parser.py`,
`test_analytics.py`) pin hand-computed golden numbers on a fixture journal.

## Notifications

- **Telegram:** create a bot with [@BotFather](https://t.me/BotFather), grab the
  token, and get your `chat_id` (e.g. via [@userinfobot](https://t.me/userinfobot)).
- **Discord:** channel → *Integrations → Webhooks → New Webhook* → copy the URL.

Both go in `config.yaml`. Channels fail independently — Discord being down never
blocks Telegram.

## Project layout

```
pine/break_signal.pine        TradingView Pine v6 indicator (Phase 1)
src/break_signal/
  core/        pivots, trendline, breakout, engine, indicators, state
  data/        okx_rest (backfill), okx_ws (live stream)
  notify/      telegram, discord (alerts); telegram_bot (commands + /ask)
  render/      mplfinance chart snapshot
  backtest/    offline replay -> CSV
  journal/     trade journal: db, parser, analytics, rules, similar, footer, memory,
               report, export, backup, tools, mcp_server (Claude Code),
               coach + prompts (Anthropic API), web + static/dashboard.html,
               webhook (TradingView), cli
  watcher.py   one (symbol, timeframe) worker
  __main__.py  entrypoint
tests/         algorithm tests on synthetic data
```
