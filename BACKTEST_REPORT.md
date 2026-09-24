# Backtest hit-rate — target 2R, stop at the line, 30-bar horizon

| market | window | signals | W/L/BE | win | avg R | total R | PF | max DD | ambig | unresolved |
|---|---|---|---|---|---|---|---|---|---|---|
| SOL-USDT-SWAP 1D | 2024-08-25 → 2026-08-24 | 31 | 14/17/0 | 45% | 0.35 | 11.00 | 1.65 | 10.00 | 1 | 1 |
| SOL-USDT-SWAP 4H | 2024-09-19 → 2026-09-19 | 189 | 62/127/0 | 33% | -0.03 | -5.11 | 0.96 | 21.15 | 7 | 0 |
| BTC-USDT-SWAP 1D | 2024-08-25 → 2026-08-24 | 25 | 14/11/0 | 56% | 0.56 | 13.98 | 2.27 | 6.00 | 0 | 0 |
| BTC-USDT-SWAP 4H | 2024-09-19 → 2026-09-19 | 231 | 88/143/0 | 38% | 0.13 | 29.45 | 1.21 | 27.20 | 4 | 2 |
| ETH-USDT-SWAP 1D | 2024-08-25 → 2026-08-24 | 34 | 19/15/0 | 56% | 0.53 | 18.09 | 2.21 | 3.00 | 1 | 0 |
| ETH-USDT-SWAP 4H | 2024-09-19 → 2026-09-19 | 233 | 88/145/0 | 38% | 0.08 | 18.43 | 1.13 | 17.47 | 6 | 0 |
| **all** | 2024-09-19 → 2026-08-24 | 743 | 285/458/0 | 38% | 0.12 | 85.84 | 1.19 | 44.91 | 19 | 3 |

3 signal(s) fired too close to the end of the data to resolve within 30 bars and are excluded rather than counted flat.
Pooled expectancy 0.12R over 743 signals (avg win 1.90R, avg loss -0.99R).
Exits: horizon 30, stop 452, target 261.
19 signal(s) hit target and stop inside one candle and were counted as losses — the true result is somewhere above this line.

## How to read this

- **Not a strategy result.** The exit rule above is a neutral proxy chosen to make
  signals comparable, not a plan anyone trades. A different stop, target or horizon
  gives different numbers; re-run with `--rr` / `--horizon` before leaning on any of it.
- **No costs.** Fees, funding and slippage are not modelled. On a market whose PF is
  near 1.0 they are enough to turn it negative.
- **One window.** Each row covers the period in its window column — one regime. A
  run over a shorter window usually just caught a friendlier one, so compare rows
  only when their windows match (`--days` makes them match across timeframes;
  `--limit` does not, because a bar count spans less time on a faster timeframe).
- **Sample size.** Per-market counts are in the table; treat anything under ~30
  signals as indicative only.
- Stop = the broken line, and a signal is judged only on candles *after* it fired, so
  there is no look-ahead. Every statistic comes from `journal/analytics.py`.
