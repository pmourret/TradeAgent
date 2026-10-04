"""Modèles mathématiques : le code calcule, les agents décident.

Chaque modèle lit les bougies terminées et rend un petit signal normalisé, le même pour tous les agents (le LLM
et ses variantes, et le témoin `quant` qui trade sur ces signaux sans LLM, donc sans loyer). Ils tournent à
chaque cycle, gratuitement, et se rejouent tels quels en backtest.

Les réglages sont des valeurs classiques, fixées à l'avance et non ajustées sur les mois de backtest : des
modèles réglés sur la période qu'on regarde paraissent excellents sur cette période et ne valent rien
ailleurs. Ce sont des modèles publics et simples : ils orientent une décision, ils ne promettent pas un gain.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from .models import TIMEFRAME_SECONDS, Candle

DAY = 86_400
TREND_FAST_DAYS = 7
TREND_SLOW_DAYS = 30
TREND_DEADBAND_PCT = 0.1        # sous cet écart, un vote est neutre
EWMA_LAMBDA = 0.94              # RiskMetrics : poids des rendements récents dans la volatilité
VOL_HIGH, VOL_LOW = 1.3, 0.7    # volatilité récente rapportée à celle du mois
EXIT_SIGMAS = 2.0               # niveau de sortie : deux mouvements journaliers types sous le prix
EXIT_MIN_PCT, EXIT_MAX_PCT = 2.0, 15.0
RISK_PER_TRADE_PCT = 1.0        # perte acceptée sur un ordre si la sortie est touchée, en % de l'equity


def _vote(value: float, reference: float) -> int:
    gap = (value / reference - 1) * 100
    return 0 if abs(gap) < TREND_DEADBAND_PCT else (1 if gap > 0 else -1)


def trend_regime(candles: list[Candle], timeframe: str) -> dict[str, Any] | None:
    """Régime de tendance : hausse, baisse ou sans direction.

    Quatre votes : le prix contre sa moyenne de 7 jours, la pente de cette moyenne sur 24 h, le prix contre sa
    moyenne de 30 jours, et la moyenne de 7 jours contre celle de 30 jours. Le score est leur moyenne, de -1 à 1.
    Les deux derniers votes n'existent que si l'historique couvre 30 jours.
    """
    step = TIMEFRAME_SECONDS[timeframe]
    per_day = max(1, DAY // step)
    fast = TREND_FAST_DAYS * DAY // step
    slow = TREND_SLOW_DAYS * DAY // step
    closes = [c.close for c in candles]
    if fast < 2 or len(closes) < fast + per_day:
        return None
    last = closes[-1]
    sma_fast = statistics.fmean(closes[-fast:])
    sma_fast_before = statistics.fmean(closes[-fast - per_day:-per_day])
    votes = [_vote(last, sma_fast), _vote(sma_fast, sma_fast_before)]
    if len(closes) >= slow:
        sma_slow = statistics.fmean(closes[-slow:])
        votes += [_vote(last, sma_slow), _vote(sma_fast, sma_slow)]
    score = statistics.fmean(votes)
    regime = "up" if score >= 0.5 else "down" if score <= -0.5 else "range"
    return {"regime": regime, "score": round(score, 2)}


def volatility_forecast(candles: list[Candle], timeframe: str) -> dict[str, Any] | None:
    """Prévision de volatilité : l'ampleur type du mouvement sur les prochaines 24 h, en % (un écart-type).

    Moyenne mobile exponentielle des rendements au carré (méthode RiskMetrics). `ratio` compare cette volatilité
    récente à celle de tout l'historique fourni : au-dessus de 1,3 le marché est agité, sous 0,7 il est calme.
    """
    step = TIMEFRAME_SECONDS[timeframe]
    per_day = max(1, DAY // step)
    closes = [c.close for c in candles if c.close > 0]
    if len(closes) < 2 * per_day + 1:
        return None
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    variance = statistics.fmean(r * r for r in returns[:per_day])     # amorce : la première journée
    for r in returns[per_day:]:
        variance = EWMA_LAMBDA * variance + (1 - EWMA_LAMBDA) * r * r
    sigma = math.sqrt(variance)
    usual = math.sqrt(statistics.fmean(r * r for r in returns))
    ratio = sigma / usual if usual > 0 else 1.0
    level = "high" if ratio >= VOL_HIGH else "low" if ratio <= VOL_LOW else "normal"
    return {"move_24h_pct": round(sigma * math.sqrt(per_day) * 100, 2), "level": level, "ratio": round(ratio, 2)}


def risk_sizing(volatility: dict[str, Any] | None) -> dict[str, Any] | None:
    """Risque et taille : où sortir, et combien miser pour qu'une sortie touchée ne coûte qu'environ 1 % de l'equity.

    Le niveau de sortie est à deux mouvements journaliers types sous le prix (entre 2 % et 15 %) : assez loin pour
    ne pas être touché par le bruit, assez près pour couper une vraie baisse.
    """
    if not volatility:
        return None
    exit_pct = min(max(EXIT_SIGMAS * volatility["move_24h_pct"], EXIT_MIN_PCT), EXIT_MAX_PCT)
    return {"exit_pct": round(exit_pct, 2), "size_pct": round(min(100.0, RISK_PER_TRADE_PCT / exit_pct * 100), 1)}


def compute_models(candles: list[Candle], timeframe: str) -> dict[str, Any]:
    """Tous les signaux d'un symbole. Un modèle qui manque d'historique est simplement absent."""
    out: dict[str, Any] = {}
    trend = trend_regime(candles, timeframe)
    if trend:
        out["trend"] = trend
    volatility = volatility_forecast(candles, timeframe)
    if volatility:
        out["vol"] = volatility
    risk = risk_sizing(volatility)
    if risk:
        out["risk"] = risk
    return out
