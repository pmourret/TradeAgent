"""Résumé compact des bougies pour l'agent.

Chaque token envoyé au LLM coûte de l'argent : on lui donne quelques chiffres utiles
(variations, extrêmes, volatilité, dernières clôtures), pas l'historique brut.

Le code calcule, le LLM interprète : les indicateurs (tendance, RSI, amplitude, position dans la fourchette)
sont faits ici, exactement, plutôt que laissés à l'arithmétique d'un modèle de langage. Sans eux, l'agent
achetait un rebond d'une heure sans voir qu'il était au sommet d'une tendance baissière (backtests réels
du 2026-10-04).
"""
from __future__ import annotations

import statistics
from typing import Any

from .models import TIMEFRAME_SECONDS, Candle

HORIZONS = {"1h": 3_600, "6h": 21_600, "24h": 86_400}
LONG_HORIZONS = {"7d": 7 * 86_400, "30d": 30 * 86_400}
SMA_WINDOWS = {"24h": 86_400, "7d": 7 * 86_400, "30d": 30 * 86_400}
MAX_CLOSES = 24
MAX_DAILY_CLOSES = 10
RSI_PERIOD = 14
ATR_PERIOD = 14


def _sig(value: float) -> float:
    """5 chiffres significatifs : 60123.456 -> 60123, 0.0123456 -> 0.012346."""
    return float(f"{value:.5g}")


def rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """RSI de Wilder, entre 0 et 100 : la part des hausses dans les mouvements récents. Au-dessus de 70 le prix
    a beaucoup monté d'affilée, sous 30 beaucoup baissé. None s'il n'y a pas `period + 1` clôtures."""
    if len(closes) < period + 1:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(c, 0.0) for c in changes[:period]) / period
    loss = sum(max(-c, 0.0) for c in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
    if gain == 0 and loss == 0:
        return 50.0
    if loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def atr_pct(candles: list[Candle], period: int = ATR_PERIOD) -> float | None:
    """Amplitude moyenne d'une bougie (« average true range »), en % du dernier prix : la taille d'un mouvement
    ordinaire. Un mouvement plus petit que ça est du bruit."""
    if len(candles) < period + 1 or candles[-1].close <= 0:
        return None
    ranges = []
    for previous, candle in zip(candles[-period - 1:], candles[-period:]):
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous.close), abs(candle.low - previous.close)))
    return statistics.fmean(ranges) / candles[-1].close * 100


def indicators(candles: list[Candle], timeframe: str) -> dict[str, Any]:
    """Tendance, RSI, amplitude et position dans la fourchette. Chaque mesure n'apparaît que si l'historique suffit."""
    if not candles:
        return {}
    step = TIMEFRAME_SECONDS[timeframe]
    closes = [c.close for c in candles]
    last = closes[-1]
    out: dict[str, Any] = {}

    long_changes = {}
    for name, seconds in LONG_HORIZONS.items():
        back = seconds // step
        if back >= 1 and len(closes) > back:
            long_changes[name] = round((last / closes[-1 - back] - 1) * 100, 2)
    if long_changes:
        out["change_long_pct"] = long_changes

    trend = {}
    for name, seconds in SMA_WINDOWS.items():
        n = seconds // step
        if n >= 2 and len(closes) >= n:
            trend[name] = round((last / statistics.fmean(closes[-n:]) - 1) * 100, 2)
    if trend:
        out["trend_vs_sma_pct"] = trend

    per_day = max(1, 86_400 // step)
    daily = closes[::-1][::per_day][::-1]            # une clôture par jour, la dernière étant le prix courant
    strength = {}
    hourly_rsi = rsi(closes[-10 * RSI_PERIOD:])
    if hourly_rsi is not None:
        strength[timeframe] = round(hourly_rsi)
    if per_day > 1:
        daily_rsi = rsi(daily)
        if daily_rsi is not None:
            strength["1d"] = round(daily_rsi)
    if strength:
        out["rsi"] = strength

    amplitude = atr_pct(candles)
    if amplitude is not None:
        out["atr_pct"] = round(amplitude, 2)

    ranges = {}
    for name, seconds in LONG_HORIZONS.items():
        n = seconds // step
        if n >= 2 and len(candles) >= n:
            window = candles[-n:]
            high, low = max(c.high for c in window), min(c.low for c in window)
            ranges[name] = {
                "high": _sig(high), "low": _sig(low),
                "pos_pct": round((last - low) / (high - low) * 100) if high > low else 50,
                "from_high_pct": round((last / high - 1) * 100, 2),
            }
    if ranges:
        out["range"] = ranges

    if per_day > 1 and len(daily) >= 3:
        out["daily_closes"] = [_sig(c) for c in daily[-MAX_DAILY_CLOSES:]]
    return out


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

    summary.update(indicators(candles, timeframe))
    summary["closes"] = [_sig(c) for c in closes[-MAX_CLOSES:]]
    return summary
