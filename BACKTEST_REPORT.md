# Backtest hit-rate — target 2R, stop at the line, 30-bar horizon

| market | signals | W/L/BE | win | avg R | total R | PF | max DD | ambig |
|---|---|---|---|---|---|---|---|---|
| SOL-USDT-SWAP 1D | 32 | 15/17/0 | 47% | 0.35 | 11.24 | 1.66 | 10.00 | 1 |
| SOL-USDT-SWAP 4H | 53 | 19/34/0 | 36% | 0.06 | 2.98 | 1.09 | 8.00 | 1 |
| BTC-USDT-SWAP 1D | 33 | 16/17/0 | 48% | 0.36 | 11.98 | 1.71 | 6.00 | 0 |
| BTC-USDT-SWAP 4H | 51 | 18/33/0 | 35% | 0.01 | 0.61 | 1.02 | 18.50 | 1 |
| ETH-USDT-SWAP 1D | 35 | 19/16/0 | 54% | 0.51 | 17.89 | 2.18 | 3.00 | 1 |
| ETH-USDT-SWAP 4H | 58 | 21/37/0 | 36% | 0.09 | 5.00 | 1.14 | 11.00 | 1 |
| **all** | 262 | 108/154/0 | 41% | 0.19 | 49.70 | 1.33 | 29.20 | 5 |

Signals from 2023-12-04 to 2026-09-21.
Pooled expectancy 0.19R over 262 signals (avg win 1.87R, avg loss -0.99R).
Exits: horizon 14, stop 151, target 97.
5 signal(s) hit target and stop inside one candle and were counted as losses — the true result is somewhere above this line.

## How to read this

- **Not a strategy result.** The exit rule above is a neutral proxy chosen to make
  signals comparable, not a plan anyone trades. A different stop, target or horizon
  gives different numbers; re-run with `--rr` / `--horizon` before leaning on any of it.
- **No costs.** Fees, funding and slippage are not modelled. On a market whose PF is
  near 1.0 they are enough to turn it negative.
- **One window.** These are the last few hundred bars, i.e. one regime. A run that
  looks better or worse over a shorter window usually just caught a friendlier one.
- **Sample size.** Per-market counts are in the table; treat anything under ~30
  signals as indicative only.
- Stop = the broken line, and a signal is judged only on candles *after* it fired, so
  there is no look-ahead. Every statistic comes from `journal/analytics.py`.
