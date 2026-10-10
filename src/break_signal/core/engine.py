"""Engine: run the full pipeline for one candle series.

Given a Candles series and Params, compute the active trendlines at the last
bar and any breakout signals there. This is what both the live watcher and the
backtester call, guaranteeing identical logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import indicators
from .breakout import check_breaks
from .params import Params
from .pivots import merge_pivots, pivot_highs, pivot_lows
from .retest import PendingRetest, check_retests
from .trendline import build_lines
from .types import Candles, Signal, Trendline


@dataclass
class EngineResult:
    lines: list[Trendline]
    signals: list[Signal]
    atr_last: float
    # (line_id, reason) for pending retests the watcher/replay should retire.
    # Always empty unless params.use_retest and pendings were passed in.
    retire: list[tuple[str, str]] = field(default_factory=list)


class Engine:
    def __init__(self, params: Params, tf_seconds: int, symbol: str, exchange: str, tf_label: str):
        self.params = params
        self.tf_seconds = tf_seconds
        self.symbol = symbol
        self.exchange = exchange
        self.tf_label = tf_label
        self.pivot_len = params.resolved_pivot_len(tf_seconds)
        # Finer second scale (fixed lookback) for multi-scale detection; the gap
        # used to cluster touches shrinks to match so close touches still count.
        self.pivot_fine = params.pivot_len_fine
        self.touch_gap = self.pivot_fine if params.use_fine_pivots else self.pivot_len

    def _confirmed(self, idxs: list[int], last_bar: int, L: int) -> list[int]:
        """Keep pivots whose right window exists (confirmed) and that aren't too old."""
        return [i for i in idxs if i <= last_bar - L and (last_bar - i) <= self.params.max_age]

    def _pivot_bars(self, values, last_bar: int, finder) -> list[int]:
        """Confirmed pivots at the coarse scale, merged with the fine scale when on."""
        coarse = self._confirmed(finder(values, self.pivot_len), last_bar, self.pivot_len)
        if not self.params.use_fine_pivots:
            if len(coarse) > self.params.max_pivots:
                coarse = coarse[-self.params.max_pivots :]
            return coarse
        fine = self._confirmed(finder(values, self.pivot_fine), last_bar, self.pivot_fine)
        return merge_pivots(coarse, fine, self.params.max_pivots)

    def evaluate(
        self,
        candles: Candles,
        broken_ids: set[str] | None = None,
        pending: list[PendingRetest] | None = None,
    ) -> EngineResult:
        """Compute lines + signals at the last bar of ``candles``.

        When ``params.use_retest`` is set, any ``pending`` retests are resolved
        against the last bar too; the resulting retest signals are appended and
        the ids to retire are returned on ``EngineResult.retire``.
        """
        broken_ids = broken_ids or set()
        n = len(candles)
        last_bar = n - 1
        atr = indicators.atr(candles.high, candles.low, candles.close, 14)
        vol_sma = indicators.sma(candles.volume, 20)
        rsi = indicators.rsi(candles.close, 14)
        atr_last = float(atr[last_bar]) if np.isfinite(atr[last_bar]) else float("nan")

        ph = self._pivot_bars(candles.high, last_bar, pivot_highs)
        pl = self._pivot_bars(candles.low, last_bar, pivot_lows)

        lines = build_lines(
            candles, atr_last, ph, pl, self.params, last_bar, self.touch_gap, broken_ids
        )
        signals = check_breaks(
            candles,
            atr_last,
            float(vol_sma[last_bar]) if np.isfinite(vol_sma[last_bar]) else float("nan"),
            float(rsi[last_bar]) if np.isfinite(rsi[last_bar]) else float("nan"),
            lines,
            self.params,
            last_bar,
            self.symbol,
            self.exchange,
            self.tf_label,
        )

        retire: list[tuple[str, str]] = []
        if self.params.use_retest and pending:
            rt = check_retests(
                candles,
                pending,
                atr_last,
                float(rsi[last_bar]) if np.isfinite(rsi[last_bar]) else float("nan"),
                self.params,
                last_bar,
                self.tf_seconds,
                self.symbol,
                self.exchange,
                self.tf_label,
            )
            signals = signals + rt.signals
            retire = rt.retire

        return EngineResult(lines=lines, signals=signals, atr_last=atr_last, retire=retire)
