"""Résumé compact des bougies pour l'agent.

Chaque token envoyé au LLM coûte de l'argent : on lui donne quelques chiffres utiles
(variations, extrêmes, volatilité, dernières clôtures), pas l'historique brut.
"""
from __future__ import annotations

import statistics
from typing import Any

from .models import TIMEFRAME_SECONDS, Candle

HORIZONS = {"1h": 3_600, "6h": 21_600, "24h": 86_400}
MAX_CLOSES = 24


def _sig(value: float) -> float:
    """5 chiffres significatifs : 60123.456 -> 60123, 0.0123456 -> 0.012346."""
    return float(f"{value:.5g}")


def summarize_candles(candles: list[Candle], timeframe: str) -> dict[str, Any]:
    if not candles:
        return {}
    step = TIMEFRAME_SECONDS[timeframe]
    closes = [c.close for c in candles]
    last = closes[-1]
    summary: dict[str, Any] = {"last": _sig(last)}

    changes = {}
    for name, seconds in HORIZONS.items():
        back = seconds // step
        if back >= 1 and len(closes) > back:
            changes[name] = round((last / closes[-1 - back] - 1) * 100, 2)
    if changes:
        summary["change_pct"] = changes

    window = candles[-max(1, 86_400 // step):]
    summary["high_24h"] = _sig(max(c.high for c in window))
    summary["low_24h"] = _sig(min(c.low for c in window))

    recent = closes[-(max(1, 86_400 // step) + 1):]
    returns = [b / a - 1 for a, b in zip(recent, recent[1:])]
    if len(returns) >= 2:
        summary["volatility_pct_per_candle"] = round(statistics.pstdev(returns) * 100, 3)

    summary["closes"] = [_sig(c) for c in closes[-MAX_CLOSES:]]
    return summary
