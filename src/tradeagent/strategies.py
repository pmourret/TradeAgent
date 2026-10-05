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


class QuantAgent:
    """Le témoin : trade sur les signaux des modèles mathématiques (`signals.py`), sans LLM, donc sans loyer.

    Achète un symbole quand son régime de tendance est à la hausse, à la taille que donne le modèle de risque ;
    vend quand le régime passe à la baisse ou quand le prix touche son niveau de sortie, qui suit le prix à la
    hausse. C'est la référence à battre pour tout agent LLM qui reçoit les mêmes signaux : s'il ne fait pas mieux,
    il n'apporte rien par-dessus les modèles.

    `trade` accepte un niveau d'exposition et une liste de symboles : c'est par là que le superviseur
    (`supervisor.py`) règle le risque sans toucher aux règles. À 1.0 et sans liste, `trade` est exactement le témoin.
    `state` reçoit les niveaux de sortie et la taille de base de chaque position : un dictionnaire que l'appelant
    peut garder en base (le témoin, lui, le garde en mémoire : il ne sert qu'au backtest).

    La taille de base (`units`) est la quantité que la position aurait à l'exposition 1. La cible est cette base
    fois l'exposition, et elle se compare à la quantité RÉELLEMENT détenue : un ordre refusé, raté ou trop petit
    pour partir ne fausse rien, il est simplement retenté tant que l'écart vaut un ordre.
    """

    name = "quant"

    def __init__(self, state: dict | None = None) -> None:
        state = state if state is not None else {}
        self._stops: dict[str, float] = state.setdefault("stops", {})
        self._units: dict[str, float] = state.setdefault("units", {})      # quantité de la position à l'exposition 1
        self._pending: dict[str, float] = state.setdefault("pending", {})  # exposition de l'achat qui vient d'être demandé

    @staticmethod
    def _models(view: MarketView, symbol: str) -> dict:
        return view.market.get(symbol, {}).get("models") or {}

    def decide(self, view: MarketView) -> Decision:
        return self.trade(view)

    def trade(self, view: MarketView, exposure: float = 1.0, allowed: frozenset[str] | None = None) -> Decision:
        """Une décision. `exposure` multiplie la taille des positions (0 = tout vendre) ; `allowed` restreint les
        symboles (None = tous). Un changement d'exposition retaille les positions ouvertes."""
        minimum = view.limits.get("min_order_quote", 0.0)
        floor = max(minimum, 0.01)
        for symbol in sorted(view.positions):          # sortir d'abord : une vente n'est jamais bloquée
            position = view.positions[symbol]
            held, price = position["value"], position["price"]
            if held < floor:
                self._stops.pop(symbol, None)
                self._units.pop(symbol, None)
                self._pending.pop(symbol, None)
                continue
            if symbol not in self._units:              # première vue de la position : sa taille de base
                self._units[symbol] = position["quantity"] / (self._pending.pop(symbol, None) or 1.0)
            models = self._models(view, symbol)
            exit_pct = (models.get("risk") or {}).get("exit_pct")
            if exit_pct:                               # le niveau de sortie suit le prix à la hausse, jamais à la baisse
                self._stops[symbol] = max(self._stops.get(symbol, 0.0), price * (1 - exit_pct / 100))
            stop = self._stops.get(symbol)
            if (models.get("trend") or {}).get("regime") == "down":
                return Decision(SELL, symbol, held, "quant: tendance à la baisse, on sort")
            if stop and price <= stop:
                return Decision(SELL, symbol, held, f"quant: niveau de sortie touché ({stop:.5g})")
            if exposure <= 0:
                return Decision(SELL, symbol, held, "superviseur: pause, on sort")
            if allowed is not None and symbol not in allowed:
                return Decision(SELL, symbol, held, "superviseur: symbole écarté, on sort")
            excess = round((position["quantity"] - self._units[symbol] * exposure) * price, 2)
            if excess >= floor and held - excess >= floor:     # une part trop petite pour un ordre n'est pas vendue
                return Decision(SELL, symbol, excess, f"superviseur: exposition x{exposure:g}, on allège")
        if exposure <= 0:
            return Decision(HOLD, reasoning="superviseur: pause, pas d'achat")
        for symbol in sorted(view.positions):
            if allowed is not None and symbol not in allowed:
                continue
            held = view.positions[symbol]["value"]
            models = self._models(view, symbol)
            risk = models.get("risk") or {}
            if (models.get("trend") or {}).get("regime") != "up" or not risk:
                continue
            if held >= floor:                          # sous sa cible : on complète, tant que la tendance tient
                position = view.positions[symbol]
                missing = (self._units.get(symbol, position["quantity"]) * exposure - position["quantity"]) * position["price"]
                extra = min(buy_room(view, symbol), round(missing, 2))
                if extra >= floor:
                    return Decision(BUY, symbol, extra, f"superviseur: exposition x{exposure:g}, on renforce")
                continue
            amount = min(buy_room(view, symbol), round(view.equity * risk["size_pct"] * exposure / 100, 2))
            if amount >= minimum and amount > 0:
                self._stops[symbol] = view.positions[symbol]["price"] * (1 - risk["exit_pct"] / 100)
                self._pending[symbol] = exposure
                return Decision(BUY, symbol, amount, f"quant: tendance à la hausse, sortie à -{risk['exit_pct']:g} %")
        return Decision(HOLD, reasoning="quant: pas de signal")


STRATEGIES = {"buyhold": BuyAndHoldAgent, "dca": DcaAgent, "momentum": MomentumAgent, "quant": QuantAgent}
