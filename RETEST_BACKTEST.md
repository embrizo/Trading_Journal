# Backtest — break vs retest, target 2R, stop at the line, 30-bar horizon

Signals generated with **strict defaults + `use_retest`**; a stop fills at the worse of the stop and the bar's open, a target at the limit. The retest is a second entry (entry = the retest close, stop = the line at the retest bar), judged by the same proxy.

Both legs are floored at a minimum risk of **0.3×ATR** (the break buffer): a break always clears it, but it drops retests that close right on the line, whose near-zero risk would otherwise put target and stop inside one bar and score as an ambiguous loss — an artifact, not an edge.

| market | kind | window | signals | W/L/BE | win | avg R | total R | PF | max DD | ambig |
|---|---|---|---|---|---|---|---|---|---|---|
| SOL-USDT-SWAP 1D | break | 2024-09-12 → 2026-09-11 | 31 | 14/17/0 | 45% | 0.35 | 11.00 | 1.65 | 8.00 | 1 |
| SOL-USDT-SWAP 1D | retest | 2024-09-12 → 2026-09-11 | 7 | 3/4/0 | 43% | 0.29 | 2.00 | 1.50 | 3.00 | 1 |
| SOL-USDT-SWAP 4H | break | 2024-10-07 → 2026-10-07 | 191 | 62/129/0 | 32% | -0.04 | -7.11 | 0.94 | 21.15 | 7 |
| SOL-USDT-SWAP 4H | retest | 2024-10-07 → 2026-10-07 | 84 | 28/56/0 | 33% | 0.00 | 0.00 | 1.00 | 14.00 | 0 |
| BTC-USDT-SWAP 1D | break | 2024-09-12 → 2026-09-11 | 25 | 14/11/0 | 56% | 0.56 | 13.98 | 2.27 | 6.58 | 0 |
| BTC-USDT-SWAP 1D | retest | 2024-09-12 → 2026-09-11 | 7 | 5/2/0 | 71% | 0.93 | 6.50 | 4.25 | 2.00 | 0 |
| BTC-USDT-SWAP 4H | break | 2024-10-07 → 2026-10-07 | 235 | 87/148/0 | 37% | 0.10 | 23.18 | 1.16 | 27.20 | 4 |
| BTC-USDT-SWAP 4H | retest | 2024-10-07 → 2026-10-07 | 91 | 32/59/0 | 35% | 0.04 | 3.27 | 1.05 | 16.00 | 3 |
| ETH-USDT-SWAP 1D | break | 2024-09-12 → 2026-09-11 | 33 | 19/14/0 | 58% | 0.58 | 19.09 | 2.36 | 4.00 | 1 |
| ETH-USDT-SWAP 1D | retest | 2024-09-12 → 2026-09-11 | 7 | 3/4/0 | 43% | 0.29 | 2.00 | 1.50 | 3.00 | 0 |
| ETH-USDT-SWAP 4H | break | 2024-10-07 → 2026-10-07 | 239 | 89/150/0 | 37% | 0.07 | 15.43 | 1.10 | 17.47 | 7 |
| ETH-USDT-SWAP 4H | retest | 2024-10-07 → 2026-10-07 | 82 | 33/49/0 | 40% | 0.19 | 15.33 | 1.31 | 6.67 | 3 |
| **all** | break | 2024-10-07 → 2026-09-11 | 754 | 285/469/0 | 38% | 0.10 | 75.57 | 1.16 | 46.38 | 20 |
| **all** | retest | 2024-10-07 → 2026-09-11 | 278 | 104/174/0 | 37% | 0.10 | 29.10 | 1.17 | 17.00 | 7 |

**Break vs retest (pooled).** Win 38% (n=754) → 37% (n=278); PF 1.16 → 1.17; expectancy 0.10R → 0.10R; total 75.57R → 29.10R. The retest fired on 37% of the breaks (278 of 754).
The retest earns a live slot only if it lifts win rate **and** expectancy enough to justify trading far fewer signals; a higher win rate on a handful of trades is noise. Read the per-market rows the same way — a retest edge that only shows up pooled, or only on one market, is not one to enable.

7 retest and 20 break signal(s) hit target and stop inside one candle and were scored as losses. The retest enters near the line, so its stop is tight and this happens to it far more often — its true result is understated relative to the break's, which the gap should be read against.

2 signal(s) fired too close to the end of the data to resolve within 30 bars and are excluded rather than counted flat.

## How to read this

- **The retest is a different trade, not a filter.** It enters later, at the retest
  close, with its stop at the line where the retest happened — so its R is measured
  against a smaller risk and the two populations are not the same bars.
- **Not a strategy result.** The exit rule is a neutral proxy, not a plan anyone trades.
  Re-run with `--rr` / `--horizon` before leaning on any of it.
- **No costs.** Fees, funding and slippage are not modelled; near PF 1.0 they decide it.
- **One window.** Compare rows only when their windows match (`--days`, not `--limit`).
- **Sample size.** Retests are rarer than breaks; treat any kind-row under ~30 signals
  as indicative only, and per-market retest counts will often be below that.
- Stop = the broken line, signals judged only on candles *after* they fired (no
  look-ahead), every statistic from `journal/analytics.py`.
