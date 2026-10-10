"""Notifier interface and the shared human-readable message formatter."""
from __future__ import annotations

from typing import Protocol

from ..core.types import Signal


class Notifier(Protocol):
    name: str

    async def send(self, text: str, image: bytes | None = None) -> None:
        ...


_ARROW = {
    "break_up": "🔴 ↑ RESISTANCE BREAK",
    "break_down": "🟢 ↓ SUPPORT BREAK",
    "retest_up": "🔴 ↑ RESISTANCE BREAK → RETEST HOLD",
    "retest_down": "🟢 ↓ SUPPORT BREAK → RETEST HOLD",
}


def format_message(sig: Signal, footer: str | None = None) -> str:
    """Alert text. ``footer`` (e.g. the journal history block) is appended
    verbatim after a blank line so this module stays independent of the journal."""
    head = _ARROW.get(sig.event, sig.event)
    body = (
        f"{head}\n"
        f"{sig.symbol} · {sig.tf} · {sig.exchange}\n\n"
        f"Price   {sig.price:.4g}\n"
        f"Line    {sig.side}, {sig.touches} touches, {sig.age_bars} bars old\n"
        f"Broke   {sig.price:.4g} vs {sig.line:.4g}  ({sig.atr_dist:.2f} ATR)\n"
        f"Volume  {sig.vol_ratio:.2f}× avg\n"
        f"RSI     {sig.rsi:.1f}\n"
        f"Time    {sig.time}"
    )
    return body + ("\n\n" + footer if footer else "")
