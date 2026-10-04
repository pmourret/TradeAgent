"""Stratégies de comparaison pour le backtest : simples, sans LLM, gratuites.

Ce sont des agents comme les autres : elles ne voient qu'une `MarketView` et rendent une `Decision`, qui
passe par les mêmes garde-fous que celle du LLM. Elles lisent leurs marges dans `view.limits` (ce que tout
agent a le droit de connaître) pour ne pas demander ce qui serait refusé à chaque cycle.

Elles gardent leur état en mémoire : elles servent au backtest, pas à `tradeagent run`.
"""
from __future__ import annotations

from .agents import MarketView
from .models import BUY, HOLD, SELL, Decision

CASH_MARGIN = 0.99      # garde de quoi payer frais et glissement sur l'ordre demandé


def buy_room(view: MarketView, symbol: str) -> float:
    """Montant qu'on peut encore acheter sur `symbol` à ce cycle d'après les limites visibles (0 si rien)."""
    limits = view.limits
    if view.risk_tier == "defensive" or limits.get("buys_left_today", 0) <= 0:
        return 0.0
    if view.equity <= limits.get("buys_blocked_below_equity", 0.0):
        return 0.0
    held = view.positions[symbol]["value"]
    exposure = sum(p["value"] for p in view.positions.values())
    room = min(
        limits.get("max_order_quote", 0.0),
        limits.get("max_position_quote_per_symbol", 0.0) - held,
        limits.get("max_total_exposure_quote", 0.0) - exposure,
        view.cash * CASH_MARGIN,
    )
    return round(room, 2) if room >= limits.get("min_order_quote", 0.0) else 0.0


class BuyAndHoldAgent:
    """Achat-conservation équipondéré : achète autant de chaque symbole que les garde-fous le permettent,
    puis ne touche plus à rien. C'est « le marché », tel qu'on a le droit de le détenir ici."""

    name = "buyhold"

    def __init__(self) -> None:
        self._done = False

    def decide(self, view: MarketView) -> Decision:
        if self._done:
            return Decision(HOLD, reasoning="buyhold: allocation faite, on conserve")
        for symbol in sorted(view.positions, key=lambda s: view.positions[s]["value"]):
            room = buy_room(view, symbol)
            if room > 0:
                return Decision(BUY, symbol, room, "buyhold: allocation initiale")
        if any(p["value"] > 0 for p in view.positions.values()):
            self._done = True       # plus de marge nulle part : l'allocation est finie, pour de bon
        return Decision(HOLD, reasoning="buyhold: rien à acheter à ce cycle")


class DcaAgent:
    """DCA (achats programmés) : achète un petit montant fixe à intervalle régulier, en alternant les symboles,
    sans regarder le prix. Lisse le prix d'entrée."""

    name = "dca"

    def __init__(self, interval_seconds: float = 86_400.0, stake_pct: float = 10.0) -> None:
        self._interval = interval_seconds
        self._pct = stake_pct
        self._last: float | None = None
        self._turn = 0

    def decide(self, view: MarketView) -> Decision:
        if self._last is not None and view.timestamp - self._last < self._interval:
            return Decision(HOLD, reasoning="dca: pas encore l'heure")
        symbols = sorted(view.positions)
        symbol = symbols[self._turn % len(symbols)]
        wanted = max(view.stake * self._pct / 100, view.limits.get("min_order_quote", 0.0))
        amount = min(wanted, buy_room(view, symbol))
        if amount < view.limits.get("min_order_quote", 0.0) or amount <= 0:
            return Decision(HOLD, reasoning="dca: plus de marge pour acheter")
        self._last = view.timestamp
        self._turn += 1
        return Decision(BUY, symbol, round(amount, 2), "dca: achat programmé")


class MomentumAgent:
    """Momentum simple (suivi de tendance) : achète ce qui a monté sur 24 h, vend ce qui baisse sur 24 h."""

    name = "momentum"

    def __init__(self, buy_above_pct: float = 2.0, sell_below_pct: float = 0.0) -> None:
        self._buy_above = buy_above_pct
        self._sell_below = sell_below_pct

    @staticmethod
    def _change_24h(view: MarketView, symbol: str) -> float | None:
        return (view.market.get(symbol, {}).get("change_pct") or {}).get("24h")

    def decide(self, view: MarketView) -> Decision:
        minimum = view.limits.get("min_order_quote", 0.0)
        for symbol in sorted(view.positions):          # sortir d'abord : une vente n'est jamais bloquée
            change = self._change_24h(view, symbol)
            held = view.positions[symbol]["value"]
            if change is not None and change < self._sell_below and held >= max(minimum, 0.01):
                return Decision(SELL, symbol, held, f"momentum: {change:+.2f} % sur 24 h, on sort")
        for symbol in sorted(view.positions):
            change = self._change_24h(view, symbol)
            if change is None or change <= self._buy_above or view.positions[symbol]["value"] >= minimum:
                continue
            room = buy_room(view, symbol)
            if room > 0:
                return Decision(BUY, symbol, room, f"momentum: {change:+.2f} % sur 24 h, on entre")
        return Decision(HOLD, reasoning="momentum: pas de signal")


STRATEGIES = {"buyhold": BuyAndHoldAgent, "dca": DcaAgent, "momentum": MomentumAgent}
