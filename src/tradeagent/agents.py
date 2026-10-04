"""Agents. Un agent reçoit une vue du marché et renvoie une `Decision`. Rien d'autre.

Il n'a accès ni à l'exchange, ni aux clés, ni à la config des garde-fous.
L'agent LLM est dans `llm_agent.py` ; ici deux agents de plomberie.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .models import BUY, HOLD, SELL, Decision


@dataclass(frozen=True)
class MarketView:
    timestamp: float
    quote_currency: str
    stake: float
    equity: float
    cash: float
    positions: dict[str, dict[str, float]]  # symbole -> {quantity, price, value}
    recent_fills: list[dict[str, Any]] = field(default_factory=list)
    limits: dict[str, float] = field(default_factory=dict)
    risk_tier: str = "normal"
    candle_timeframe: str = ""
    market: dict[str, dict[str, Any]] = field(default_factory=dict)  # symbole -> résumé de bougies

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Agent(Protocol):
    name: str

    def decide(self, view: MarketView) -> Decision: ...


class HoldAgent:
    """Ne fait jamais rien. Sert de référence : la stratégie à battre est « ne rien faire »."""

    name = "hold"

    def decide(self, view: MarketView) -> Decision:
        return Decision(HOLD, reasoning="hold agent: no action")


class ChaosAgent:
    """Agent aléatoire pour tester la plomberie et montrer les garde-fous en action.

    Il demande aussi des choses absurdes (symbole inconnu, montant démesuré, vente de ce
    qu'il n'a pas). Il perdra des frais : il n'est pas là pour gagner.
    """

    name = "chaos"

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)

    def decide(self, view: MarketView) -> Decision:
        rng = self._rng
        symbols = list(view.positions)
        symbol = rng.choice(symbols)
        roll = rng.random()
        if roll < 0.50:
            return Decision(HOLD, reasoning="chaos: patience")
        if roll < 0.55:
            return Decision(BUY, "DOGE/EUR", 10.0, "chaos: symbole hors liste")
        if roll < 0.65:
            return Decision(BUY, symbol, view.equity * 10, "chaos: bien trop gourmand")
        if roll < 0.82:
            return Decision(BUY, symbol, round(rng.uniform(5, 30), 2), "chaos: achat aléatoire")
        held_value = view.positions[symbol]["value"]
        amount = round(rng.uniform(5, max(held_value * 1.5, 10)), 2)
        return Decision(SELL, symbol, amount, "chaos: vente aléatoire")


AGENTS = {"hold": HoldAgent, "chaos": ChaosAgent}
