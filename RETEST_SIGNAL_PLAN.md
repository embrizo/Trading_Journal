# Break-and-retest signal — implementation plan

A second, higher-conviction signal to sit alongside the existing break: price
closes through a trendline (the **break**, which we already detect), then pulls
back to the now-flipped line, tags it, and **holds** — closing back on the
breakout side instead of falling through. That hold is the **retest**. It is the
pattern drawn on the SUI 4H reference (break above the rising line → pull back to
it → continue).

This plan is additive and **off by default** (`use_retest: false`). The current
break behaviour, the strict defaults, and the "1D only" watch decision are
untouched until a backtest (phase R2) shows the retest earns a live slot.

---

## 1. Why it needs more than a new filter

The break is terminal in the current design:

- `_on_close` ([watcher.py](src/break_signal/watcher.py)) calls
  `state.mark_broken(symbol, tf, line_id)` the moment a break fires.
- `evaluate` feeds those `broken_ids` into `build_lines`, which **excludes**
  them ([trendline.py](src/break_signal/core/trendline.py) `_select_top`), so a
  broken line is dropped from the active set forever.

A retest happens *after* the break, measured *against the line that broke*. So
the line's geometry has to survive the break for a while. The lifecycle gains a
middle state:

```
ACTIVE ──break──▶ PENDING_RETEST ──▶ { RETESTED | INVALIDATED | EXPIRED } ──▶ retired
```

`broken_lines` stays exactly as it is (the "never re-arm this as a fresh break"
ledger that keeps `build_lines` from re-selecting it). What is new is a separate
record of *pending retests* that carries the broken line's frozen geometry and is
checked on each later confirmed bar.

**Freeze the geometry, don't rebuild it.** A broken line's anchor pivots can age
out of the window, so instead of relying on `build_lines` to reconstruct it we
store `(ts_ref, value_ref, slope)` at break time and recompute the line value at
any later bar as `value_ref + slope × bars_between(ts_ref, bar)`. This matches
how `line_id` is already timestamp-keyed and is robust to pivots dropping.

---

## 2. Pattern definition (computable, no discretion)

After a break of line `L` on bar `b`, for a later confirmed bar `i` with
`b < i ≤ b + retest_window`:

**Resistance break-up → `retest_up`** (long context):
- **Tagged the line:** `low[i] ≤ L.value_at(i) + retest_touch × atr` — the wick
  came back down to the flipped level.
- **Held above it:** `close[i] ≥ L.value_at(i) + retest_hold_buf × atr` — the
  close stayed on the breakout side.
- *(optional)* **Rejection body** when `retest_reject_body`: the close is in the
  upper half of the bar, `close[i] ≥ (high[i] + low[i]) / 2`.

**Support break-down → `retest_down`** (short context): the mirror — `high[i] ≥
L.value_at(i) − retest_touch × atr`, `close[i] ≤ L.value_at(i) − retest_hold_buf
× atr`, optional close in the lower half.

**Invalidation** (the breakout failed — stop watching, no signal): a close
decisively back through the line, `close[i] < L.value_at(i) − retest_invalidate ×
atr` for an up-break (mirror for down). Retire the pending.

**Expiry:** neither retest nor invalidation within `retest_window` bars → retire
(the move ran away without offering a retest; no second entry).

**One retest per line:** the first clean retest fires, then the pending is
retired. (A toggle for multiple could come later; default one.)

**No volume filter on the retest** (resolved — §7.2): a healthy pullback is
usually lower volume, so requiring a spike would reject most real retests. The
hold rule is the quality bar. The break leg keeps the full volume + body filter.

---

## 3. Parameters (new — `Params`, `config.example.yaml`, Pine inputs in parity)

| param | default | meaning |
|---|---|---|
| `use_retest` | `false` | master switch; off = today's behaviour exactly |
| `retest_window` | `12` | bars after the break to watch (~2 days on 4H) |
| `retest_touch` | `0.25` | xATR tolerance for the wick tagging the line |
| `retest_hold_buf` | `0.0` | xATR the close must stay beyond the line to "hold" |
| `retest_invalidate` | `0.30` | xATR close back through the line that voids the break |
| `retest_reject_body` | `false` | require a rejection body (close in the favourable half) |

`Params` already filters unknown yaml keys in `Config.to_params`, so adding these
is backward compatible. Keep the Pine `input.*` names and defaults identical, per
the parity rule in `CLAUDE.md`.

---

## 4. Events and the Signal shape (no schema change)

- New `event` values `retest_up` / `retest_down`; `side` stays
  `resistance`/`support`. Widen the docstring in
  [core/types.py](src/break_signal/core/types.py); `Signal.to_dict` is unchanged
  because `event` is a free string.
- For a retest signal, `price` = the retest close, `line` = the line value at the
  retest bar, `atr_dist` = `|close − line| / atr`, `age_bars` = bars since the
  line's anchor (as today). `touches` carries over from the broken line. No new
  fields in v1 — everything the journal, dashboard and coach read already exists.
- The journal `signals` table stores arbitrary `event` strings and is UNIQUE on
  `(symbol, tf, line_id, candle_ts)`; a break and its retest differ in
  `candle_ts`, so they are distinct rows on the same `line_id`. Nothing to
  migrate.

---

## 5. Engine changes (pure, backtestable)

Keep `evaluate` deterministic and side-effect-free — the backtest depends on it.

- **New input** alongside `broken_ids`: `pending: list[PendingRetest]` where
  `PendingRetest = (line_id, side, ts_ref, value_ref, slope, touches, broke_ts)`.
- **New step** `check_retests(candles, pending, atr_last, rsi_last, params,
  last_bar, …) -> RetestResult` where `RetestResult` holds `signals:
  list[Signal]` and `retire: list[(line_id, reason)]` with reason in
  `{retested, invalidated, expired}`. Lives in a new
  [core/retest.py](src/break_signal/core/retest.py), mirroring
  [core/breakout.py](src/break_signal/core/breakout.py).
- `Engine.evaluate` calls `check_breaks` then, when `use_retest`, `check_retests`,
  and returns both the break and retest signals plus the retire list on
  `EngineResult`. When `use_retest` is false it does nothing new — zero behaviour
  change.

`check_retests` recomputes the line value from the frozen `(ts_ref, value_ref,
slope)` using `tf_seconds`, so it never touches `build_lines` and never depends on
the pivots still being present.

---

## 6. Wiring the rest

- **State** ([core/state.py](src/break_signal/core/state.py)): new table
  `pending_retests(symbol, tf, line_id, side, ts_ref, value_ref, slope, touches,
  broke_ts, PRIMARY KEY(symbol, tf, line_id))`. On a break, insert a pending row
  (in addition to `mark_broken`). Methods: `pending_retests(symbol, tf)`,
  `retire_pending(symbol, tf, line_id)`. Survives restarts like `broken_lines`.
- **Watcher** ([watcher.py](src/break_signal/watcher.py) `_on_close`): load
  pendings, pass them to `evaluate`, dispatch any retest signals through the same
  `_journal_signal` → `_dispatch` path the break uses, then apply the engine's
  retire list. Record a pending whenever a break fires.
- **Notification** ([notify/base.py](src/break_signal/notify/base.py)): add
  `retest_up` / `retest_down` to `_ARROW` (e.g. `"🔴 ↑ RESISTANCE BREAK → RETEST
  HOLD"`). The message body already works for any event.
- **Replay** ([backtest/replay.py](src/break_signal/backtest/replay.py)): the
  growing-window walk already reconstructs `broken` ids as it goes; it needs to
  reconstruct pendings the same way — record a pending on each break, feed them to
  `evaluate`, apply retires. This is what lets the M6 report compare break vs
  retest.
- **Journal / coach / dashboard:** nothing to build. `analytics.signal_history`
  and `footer.py` already take an `event` filter, so `/ask "how do my retest
  entries do?"` and the alert footer work once there are trades; derived memories
  split break vs retest naturally at n≥5; the dashboard's alert markers get a new
  event value and can be given a distinct shape in `dashboard.html` if wanted.

---

## 7. Decisions (resolved 2026-10-10)

1. **Hold strictness — minimal.** A retest fires when the wick tags the line
   (within `retest_touch × atr`) and the candle closes on the breakout side. The
   `retest_reject_body` toggle still ships (default `false`) so a stricter
   rejection-body rule can be switched on from config later without a rebuild.
2. **Volume on the retest — not required.** A healthy pullback is usually quiet;
   demanding a volume spike would reject most real retests. The hold rule carries
   the quality bar. (The *break* still passes the full volume + body filter as
   today; only the retest leg skips it.)
3. **Window — `retest_window = 12` bars** as the starting default; revisit per
   timeframe if R2 shows it matters. (Still a tuning knob, not a confirmed final.)
4. **Off by default.** Break behaviour and the "1D only" watch are untouched until
   R2's backtest justifies enabling the retest. Confirmed.

---

## 8. Phases

- **R0 — core, off by default.** `core/retest.py`, `Params` fields, `State`
  table, `Engine.evaluate` branch, events, `notify/base` text. Synthetic-candle
  tests: clean break→retest→hold fires one signal; pullback that closes through →
  invalidation, no signal; run-away → expiry; symmetric support case; frozen
  geometry survives pivots aging out. No live, no Pine.
- **R1 — watcher + journal flow.** Wire `_on_close`, dispatch, pending lifecycle.
  Verify on Binance 4H (OKX is blocked here) that a historical SUI break→retest
  produces the two signals in the right order, with the second as `retest_up`.
- **R2 — the empirical case.** Extend `backtest/report.py` to group rows by
  `event`, run break vs retest across SOL/BTC/ETH over a common window, and see
  whether the retest raises win rate / PF and at what cost in signal count.
  **This decides whether it goes live**, the same way R2 decided the 4H watch.
- **R3 — Pine parity.** Add retest detection on the frozen ghost line and the new
  `alertMsg` events; visual check on TradingView against the Python output.
- **R4 — optional live.** Enable on the markets/timeframes R2 showed an edge on.

---

## 9. Risks

- **Parity drift.** Two engines must agree on the retest exactly as they do on
  the break; the frozen-geometry approach has to match Pine's ghost-line maths
  bar for bar. Covered by R3's visual check and a documented parity case.
- **Over-fitting the thresholds.** `retest_touch` / `retest_hold_buf` are easy to
  tune until the backtest looks good. R2 must report the signal count alongside
  the edge, and the thresholds should be set once and left — not swept until a
  number pleases.
- **Rarity.** Clean retests are much rarer than breaks, so per-market samples will
  be small; R2 should say so and treat anything under ~30 as indicative, matching
  the M6 report's own caveat.
