"""Market-data layer: REST backfill + live WebSocket candle stream.

Public market data only — no API key, no orders. Isolated here so the exchange
can be swapped without touching ``core/``.

Two providers implement the same pair of functions:

==========  ===================  ==========================
exchange    REST                 WebSocket
==========  ===================  ==========================
``okx``     :mod:`okx_rest`      :mod:`okx_ws`
``binance`` :mod:`binance_rest`  :mod:`binance_ws`
==========  ===================  ==========================

Pick one with ``exchange:`` in ``config.yaml`` and reach it through
:func:`provider`; callers then never import an exchange module directly.
Symbols stay canonical OKX-style instIds (``SOL-USDT-SWAP``) everywhere — in the
config, the journal and the Pine script — and each provider translates at its own
edge, so switching exchange never rewrites stored data.
"""
from __future__ import annotations

from types import ModuleType

EXCHANGES = ("okx", "binance")


def provider(exchange: str | None = None) -> tuple[ModuleType, ModuleType]:
    """Return ``(rest, ws)`` modules for ``exchange`` (default ``okx``)."""
    name = (exchange or "okx").strip().lower()
    if name == "okx":
        from . import okx_rest, okx_ws
        return okx_rest, okx_ws
    if name == "binance":
        from . import binance_rest, binance_ws
        return binance_rest, binance_ws
    raise ValueError(f"unknown exchange {exchange!r} — one of {', '.join(EXCHANGES)}")


def rest(exchange: str | None = None) -> ModuleType:
    """The REST module for ``exchange`` — it exposes ``fetch_candles`` and ``REST_URL``."""
    return provider(exchange)[0]


def ws(exchange: str | None = None) -> ModuleType:
    """The WebSocket module for ``exchange`` — it exposes ``stream_closed_candles``."""
    return provider(exchange)[1]
